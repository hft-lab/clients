import asyncio
import datetime
import hashlib
import hmac
import threading
import time
import traceback
import uuid
from urllib.parse import urlencode

import aiohttp
import orjson
import requests
from config import Config
from core.base_client import BaseClient
from clients.enums import ConnectMethodEnum, EventTypeEnum, PositionSideEnum, ResponseStatus, OrderStatus, \
    ClientsOrderStatuses


class BinanceClient(BaseClient):
    BASE_WS = 'wss://fstream.binance.com/ws/'
    BASE_URL = 'https://fapi.binance.com'
    EXCHANGE_NAME = 'BINANCE'

    def __init__(self, keys, leverage):
        self.taker_fee = 0.00036
        self.leverage = leverage
        self.symbol = keys['symbol']
        self.__api_key = keys['api_key']
        self.__secret_key = keys['secret_key']
        self.headers = {'X-MBX-APIKEY': self.__api_key}
        self.symbol_is_active = False
        self.count_flag = False
        self.error_info = None
        self.quantity_precision = 0
        self.tick_size = None
        self.step_size = None
        self.message_to_rabbit_list = []
        self.balance = {
            'total': 0.0,
            'avl_balance': 0.0
        }
        self.last_price = {
            'sell': 0,
            'buy': 0
        }
        self.orders = {}
        self.positions = {
            self.symbol: {
                'amount': 0,
                'entry_price': 0,
                'unrealized_pnl_usd': 0,
                'side': 'LONG',
                'amount_usd': 0,
                'realized_pnl_usd': 0
            }
        }
        self.orderbook = {
            self.symbol: {
                'asks': [],
                'bids': [],
                'timestamp': 0
            }
        }
        self.expect_amount_coin = 0
        self.expect_price = 0

        self._loop_public = asyncio.new_event_loop()
        self._loop_private = asyncio.new_event_loop()
        self._loop_message = asyncio.new_event_loop()
        self.wsd_public = threading.Thread(target=self._run_forever,
                                           args=[ConnectMethodEnum.PUBLIC, self._loop_public],
                                           daemon=True)
        self.wsu_private = threading.Thread(target=self._run_forever,
                                            args=[ConnectMethodEnum.PRIVATE, self._loop_private],
                                            daemon=True)
        self.bal_check = threading.Thread(target=self._balance)
        self.req_check = threading.Thread(target=self._check_symbol_value)
        self.lk_check = threading.Thread(target=self._ping_listen_key)
        asyncio.run(self.get_orderbook_by_symbol(self.symbol))

        self.balance['total'], self.balance['avl_balance'] = self._get_balance()

        self._get_listen_key()
        self._get_position()

    async def create_order(self, price, side, session: aiohttp.ClientSession,
                           expire: int = 100, client_id: str = None) -> dict:
        return await self.__create_order(price, side.upper(), session, expire, client_id)

    def cancel_all_orders(self, orderID=None) -> dict:
        return self.__cancel_open_orders()

    def get_positions(self) -> dict:
        return self.positions

    def get_real_balance(self) -> float:
        return self.balance['total']

    def get_orderbook(self) -> dict:
        while not self.orderbook.get(self.symbol):
            time.sleep(0.001)
        return self.orderbook

    def get_last_price(self, side: str) -> float:
        return self.last_price[side.lower()]

    def run_updater(self) -> None:
        self.req_check.start()
        self.lk_check.start()
        self.bal_check.start()
        self.wsd_public.start()
        self.wsu_private.start()
        # self.wsd_public.join()

    def _run_forever(self, type, loop) -> None:
        while True:
            try:
                loop.run_until_complete(self._run_loop(type))
            except Exception:  # noqa
                traceback.print_exc()

    async def _run_loop(self, type) -> None:
        async with aiohttp.ClientSession() as session:
            if type == ConnectMethodEnum.PUBLIC and self.symbol_is_active:
                await self._symbol_data_getter(session)
            elif type == ConnectMethodEnum.PRIVATE and self.listen_key:
                await self._user_data_getter(session)

    # PUBLIC -----------------------------------------------------------------------------------------------------------
    def _check_symbol_value(self) -> None:
        url_path = '/fapi/v1/exchangeInfo'
        while True:
            if response := requests.get(self.BASE_URL + url_path).json():
                for data in response['symbols']:
                    if data['symbol'] == self.symbol.upper() and data['status'] == 'TRADING' and \
                            data['contractType'] == 'PERPETUAL':
                        self.quantity_precision = data['quantityPrecision']
                        self.price_precision = data['pricePrecision']
                        self.symbol_is_active = True
                        for f in data['filters']:
                            if f['filterType'] == 'PRICE_FILTER':
                                self.tick_size = float(f['tickSize'])
                            elif f['filterType'] == 'LOT_SIZE':
                                self.step_size = float(f['stepSize'])
                        break
            else:
                self.symbol_is_active = False
            time.sleep(5)

    def __check_ob(self, ob: dict, side: str) -> None:
        if not len(self.orderbook[self.symbol][side]):
            for new_order in [[float(x[0]), float(x[1])] for x in ob.get(side, [])]:
                if new_order[1] > 0:
                    self.orderbook[self.symbol][side].append(new_order)
                    break
        for new_order in [[float(x[0]), float(x[1])] for x in ob.get(side, [])]:
            index = 0
            if side == 'bids':
                for ob_order in self.orderbook[self.symbol][side]:
                    if new_order[0] < ob_order[0]:
                        index += 1
                    elif new_order[0] == ob_order[0]:
                        if new_order[1] > 0:
                            self.orderbook[self.symbol][side][index] = new_order
                        else:
                            self.orderbook[self.symbol][side].pop(index)
                        break
                    elif new_order[0] > ob_order[0] and new_order[1] > 0:
                        self.orderbook[self.symbol][side].insert(index, new_order)
                        break
            elif side == 'asks':
                for ob_order in self.orderbook[self.symbol][side]:
                    if new_order[0] > ob_order[0]:
                        index += 1
                    elif new_order[0] == ob_order[0]:
                        if new_order[1] > 0:
                            self.orderbook[self.symbol][side][index] = new_order
                        else:
                            self.orderbook[self.symbol][side].pop(index)
                        break
                    elif new_order[0] < ob_order[0] and new_order[1] > 0:
                        self.orderbook[self.symbol][side].insert(index, new_order)
                        break

    def __orderbook_update(self, ob: dict) -> None:
        # time_start = time.time()
        last_ob = self.orderbook[self.symbol]
        if ob.get('asks'):
            self.__check_ob(ob, 'asks')
        if ob.get('bids'):
            self.__check_ob(ob, 'bids')
        if last_ob['asks'][0][0] != self.orderbook[self.symbol]['asks'][0][0] \
                or last_ob['bids'][0][0] != self.orderbook[self.symbol]['bids'][0][0]:
            self.orderbook[self.symbol]['timestamp'] = int(time.time() * 1000)
            self.count_flag = True
        # print(f"\nBINANCE NEW OB APPEND TIME: {time.time() - time_start} sec\n{self.orderbook[self.symbol]}\n")

    async def _symbol_data_getter(self, session: aiohttp.ClientSession) -> None:
        async with session.ws_connect(self.BASE_WS + self.symbol.lower()) as ws:
            await ws.send_str(orjson.dumps({
                'id': 1,
                'method': 'SUBSCRIBE',
                'params': [f"{self.symbol.lower()}@depth@100ms"]
            }).decode('utf-8'))

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    payload = orjson.loads(msg.data)
                    if payload.get('e') == 'depthUpdate':
                        payload['asks'] = [x for x in payload.get('a', [])][:10]
                        payload['bids'] = [x for x in payload.get('b', [])][::-1][:10]
                        try:
                            self.__orderbook_update(payload)
                        except:
                            pass

    # PRIVATE ----------------------------------------------------------------------------------------------------------
    @staticmethod
    def _prepare_query(params: dict) -> str:
        return urlencode(params)

    def get_available_balance(self, side):
        position_value = 0
        position_value_abs = 0
        for symbol, position in self.positions.items():
            if position.get('amount_usd'):
                position_value += position['amount_usd']
                position_value_abs += abs(position['amount_usd'])

        available_margin = self.balance['total'] * self.leverage
        if position_value_abs > available_margin:
            if position_value > 0:
                if side == 'buy':
                    return available_margin - position_value
                elif side == 'sell':
                    return available_margin + position_value
            else:
                if side == 'buy':
                    return available_margin + abs(position_value)
                elif side == 'sell':
                    return available_margin - abs(position_value)
        if side == 'buy':
            return available_margin - position_value
        elif side == 'sell':
            return available_margin + position_value

    def _create_signature(self, query: str) -> str:
        if self.__secret_key is None or self.__secret_key == "":
            raise Exception("Secret key are required")

        return hmac.new(self.__secret_key.encode(), msg=query.encode(), digestmod=hashlib.sha256).hexdigest()

    def _balance(self) -> None:
        while True:
            self.balance['total'], self.balance['avl_balance'] = self._get_balance()
            time.sleep(5)

    def _get_position(self):
        url_path = "/fapi/v2/account"
        payload = {"timestamp": int(time.time() * 1000)}

        query_string = self._prepare_query(payload)
        payload["signature"] = self._create_signature(query_string)
        query_string = self._prepare_query(payload)

        res = requests.get(url=self.BASE_URL + url_path + '?' + query_string, headers=self.headers).json()

        if isinstance(res, dict):
            for s in res.get('positions', []):
                if float(s['positionAmt']):
                    self.positions.update({s['symbol']: {
                        'side': PositionSideEnum.LONG if float(s['positionAmt']) > 0 else PositionSideEnum.SHORT,
                        'amount_usd': float(s['positionAmt']) * float(s['entryPrice']),
                        'amount': float(s['positionAmt']),
                        'entry_price': float(s['entryPrice']),
                        'unrealized_pnl_usd': float(s['unrealizedProfit']),
                        'realized_pnl_usd': 0,
                        'lever': self.leverage
                    }})
        else:
            time.sleep(5)
            self._get_position()

    def _get_balance(self) -> [float, float]:
        url_path = "/fapi/v2/balance"
        payload = {
            "timestamp": int(time.time() * 1000),
            "recvWindow": int((time.time() + 2) * 1000)
        }

        query_string = self._prepare_query(payload)
        payload["signature"] = self._create_signature(query_string)
        query_string = self._prepare_query(payload)

        res = requests.get(url=self.BASE_URL + url_path + '?' + query_string, headers=self.headers).json()
        if isinstance(res, list):
            for s in res:
                if s['asset'] == 'USDT':
                    return float(s['balance']) + float(s['crossUnPnl']), float(s['availableBalance'])
        else:
            print(res)
            time.sleep(5)
            return self._get_balance()

    async def get_historical_price(self, session, symbol, time_):
        url_path = "/fapi/v1/markPriceKlines"
        payload = {
            'symbol': symbol,
            "interval": '1m',
            "startTime": time_ - 3,
            'endTime': time_ + 3
        }
        query_string = self._prepare_query(payload)
        payload["signature"] = self._create_signature(query_string)
        query_string = self._prepare_query(payload)

        async with session.get(url=self.BASE_URL + url_path + '?' + query_string, headers=self.headers) as resp:
            price = await resp.json()
            if price:
                price = (float(price[0][1]) + float(price[0][4])) / 2
                return price
            else:
                time_ = int(time_ / 10000) * 10000
                return await self.get_historical_price(session, symbol, time_)

    async def get_funding_history(self, session, symbol):
        url_path = "/fapi/v1/fundingRate"
        payload = {
            "timestamp": int(time.time() * 1000),
            'symbol': symbol,
            'limit': 100
        }

        query_string = self._prepare_query(payload)
        payload["signature"] = self._create_signature(query_string)
        query_string = self._prepare_query(payload)

        async with session.get(url=self.BASE_URL + url_path + '?' + query_string, headers=self.headers) as resp:
            return await resp.json()

    async def get_funding_payments(self, session):
        url_path = "/fapi/v1/income"
        payload = {
            'incomeType': 'FUNDING_FEE',
            "timestamp": int(time.time() * 1000),
            "recvWindow": int((time.time() + 2) * 1000),
            'limit': 100
        }
        query_string = self._prepare_query(payload)
        payload["signature"] = self._create_signature(query_string)
        query_string = self._prepare_query(payload)
        symbols = {}

        async with session.get(url=self.BASE_URL + url_path + '?' + query_string, headers=self.headers) as resp:
            funding_payments = await resp.json()

            for fund in funding_payments:
                symbol = fund['symbol']

                if not symbols.get(symbol):
                    symbols.update({symbol: await self.get_funding_history(session, symbol)})

                rate = [x['fundingRate'] for x in symbols[symbol] if abs(x['fundingTime'] - fund['time']) < 3000]

                if rate:
                    price = await self.get_historical_price(session, symbol, fund['time'])
                    fund.update({'rate': rate[0],
                                 'price': price,
                                 'positionSize': (float(fund['income']) / -float(rate[0])) / price,
                                 'market': symbol,
                                 'payment': fund['income'],
                                 'datetime': datetime.datetime.fromtimestamp(fund['time'] / 1000),
                                 'asset': 'USDT'})

        return funding_payments

    def fit_amount(self, amount) -> None:
        self.expect_amount_coin = float(
            round(float(round(amount / self.step_size) * self.step_size), self.quantity_precision))

    async def __create_order(self, price: float, side: str, session: aiohttp.ClientSession,
                             expire=5000, client_id=None) -> dict:
        self.expect_price = float(round(float(round(price / self.tick_size) * self.tick_size), self.price_precision))
        url_path = '/fapi/v1/order?'
        query_string = f"timestamp={int(time.time() * 1000)}&symbol={self.symbol}&side={side}&type=LIMIT&" \
                       f"price={self.expect_price}&quantity={self.expect_amount_coin}&timeInForce=GTC&" \
                       f"recvWindow={time.time() * 1000 + expire}&newClientOrderId={client_id}"
        query_string += f'&signature={self._create_signature(query_string)}'

        async with session.post(url=self.BASE_URL + url_path + query_string, headers=self.headers) as resp:
            res = await resp.json()
            print(f'BINANCE RESPONSE: {res}')
            self.LAST_ORDER_ID = res.get('orderId', 'default')
            timestamp = 0000000000000
            if res.get('code') and -5023 < res['code'] < -1099:
                status = ResponseStatus.ERROR
                self.error_info = res
            elif res.get('status'):
                status = ResponseStatus.SUCCESS
                timestamp = res['updateTime']
            else:
                status = ResponseStatus.NO_CONNECTION

            return {
                'exchange_name': self.EXCHANGE_NAME,
                'timestamp': timestamp,
                'status': status
            }

    def __cancel_open_orders(self) -> dict:
        url_path = "/fapi/v1/allOpenOrders"
        payload = {
            "timestamp": int(time.time() * 1000),
            'symbol': self.symbol
        }

        query_string = self._prepare_query(payload)
        payload["signature"] = self._create_signature(query_string)
        query_string = self._prepare_query(payload)
        res = requests.delete(url=self.BASE_URL + url_path + '?' + query_string, headers=self.headers).json()
        return res

    async def get_orderbook_by_symbol(self, symbol) -> None:
        async with aiohttp.ClientSession() as session:
            url_path = "/fapi/v1/depth"
            payload = {
                "symbol": symbol,
            }

            query_string = self._prepare_query(payload)

            async with session.get(url=self.BASE_URL + url_path + "?" + query_string, headers=self.headers) as resp:
                res = await resp.json()

                if 'asks' in res and 'bids' in res:
                    self.orderbook[symbol] = {
                        'asks': [[float(x[0]), float(x[1])] for x in res['asks']][:10],
                        'bids': [[float(x[0]), float(x[1])] for x in res['bids']][:10],
                        'timestamp': time.time()
                    }

    async def get_all_orders(self, symbol: str, session: aiohttp.ClientSession) -> list:
        url_path = "/fapi/v1/allOrders"
        payload = {
            "timestamp": int(time.time() * 1000),
            "symbol": symbol,
            "startTime": int((time.time() - 86400) * 1000),
            "recvWindow": int((time.time() + 2) * 1000)
        }
        query_string = self._prepare_query(payload)
        payload["signature"] = self._create_signature(query_string)
        query_string = self._prepare_query(payload)
        orders = []
        async with session.get(url=self.BASE_URL + url_path + "?" + query_string, headers=self.headers) as resp:
            res = await resp.json()
            try:
                for order in res:
                    if res.get('status') == ClientsOrderStatuses.FILLED and float(res['origQty']) > float(
                            res['executedQty']):
                        status = OrderStatus.PARTIALLY_EXECUTED
                    elif res.get('status') == ClientsOrderStatuses.FILLED:
                        status = OrderStatus.FULLY_EXECUTED
                    else:
                        status = OrderStatus.NOT_EXECUTED

                    orders.append(
                        {
                            'id': uuid.uuid4(),
                            'datetime': datetime.datetime.fromtimestamp(order['time']),
                            'ts': int(order['time']),
                            'context': 'web-interface' if not 'api_' in order['clientOrderId'] else
                            order['clientOrderId'].split('_')[1],
                            'parent_id': '-',
                            'exchange_order_id': order['orderId'],
                            'type': order['timeInForce'],
                            'status': status,
                            'exchange': self.EXCHANGE_NAME,
                            'side': order['side'].lower(),
                            'symbol': self.symbol,
                            'expect_price': float(order['price']),
                            'expect_amount_coin': float(order['origQty']),
                            'expect_amount_usd': float(order['price']) * float(order['origQty']),
                            'except_fee': self.taker_fee,
                            'factual_price': float(order['avgPrice']),
                            'factual_amount_coin': float(order['executedQty']),
                            'order_place_time': 0,
                            'env': '-',
                            'datetime_update': datetime.datetime.utcnow(),
                            'ts_update': time.time(),
                            'client_id': order['clientOrderId']
                        }
                    )
            except:
                traceback.print_exc()

        return orders

    async def get_order_by_id(self, symbol, order_id: str, session: aiohttp.ClientSession) -> dict:
        url_path = "/fapi/v1/order"
        payload = {
            "timestamp": int(time.time() * 1000),
            "symbol": symbol,
            "orderId": int(order_id),
            "recvWindow": int((time.time() + 2) * 1000)
        }

        query_string = self._prepare_query(payload)
        payload["signature"] = self._create_signature(query_string)
        query_string = self._prepare_query(payload)

        async with session.get(url=self.BASE_URL + url_path + "?" + query_string, headers=self.headers) as resp:
            res = await resp.json()

            if res.get('status') == ClientsOrderStatuses.FILLED and res.get('time', 0) == res.get('updateTime'):
                status = OrderStatus.FULLY_EXECUTED
            elif res.get('status') == ClientsOrderStatuses.FILLED and float(res['origQty']) > float(
                    res['executedQty']):
                status = OrderStatus.PARTIALLY_EXECUTED
            else:
                status = OrderStatus.NOT_EXECUTED

            return {
                'exchange_order_id': order_id,
                'exchange': self.EXCHANGE_NAME,
                'status': status,
                'factual_price': float(res['avgPrice']),
                'factual_amount_coin': float(res['executedQty']),
                'factual_amount_usd': float(res['executedQty']) * float(res['avgPrice']),
                'datetime_update': datetime.datetime.utcnow(),
                'ts_update': time.time() * 1000
            }

    def _get_listen_key(self) -> None:
        response = requests.post(
            self.BASE_URL + '/fapi/v1/listenKey', headers=self.headers).json()

        if response.get('code'):
            raise Exception(f'ListenKey Error: {response}')
        self.listen_key = response.get('listenKey')

    def _ping_listen_key(self) -> None:
        while True:
            time.sleep(59 * 60)
            requests.put(self.BASE_URL + '/fapi/v1/listenKey', headers={'X-MBX-APIKEY': self.__api_key})

    async def _user_data_getter(self, session: aiohttp.ClientSession) -> None:
        async with session.ws_connect(self.BASE_WS + self.listen_key) as ws:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = orjson.loads(msg.data)
                    if data['e'] == EventTypeEnum.ACCOUNT_UPDATE and data['a']['P']:
                        for p in data['a']['P']:
                            if p['ps'] in PositionSideEnum.all_position_sides() and float(p['pa']):
                                self.positions.update({p['s'].upper(): {
                                    'side': PositionSideEnum.LONG if float(p['pa']) > 0 else PositionSideEnum.SHORT,
                                    'amount_usd': float(p['pa']) * float(p['ep']),
                                    'amount': float(p['pa']),
                                    'entry_price': float(p['ep']),
                                    'unrealized_pnl_usd': float(p['up']),
                                    'realized_pnl_usd': 0,
                                    'lever': self.leverage
                                }})
                    elif data['e'] == EventTypeEnum.ORDER_TRADE_UPDATE:
                        print(f'{data=}')
                        self.last_price[data['o']['S'].lower()] = float(data['o']['ap'])
                        status = None
                        if data['o']['X'] == ClientsOrderStatuses.NEW:
                            status = OrderStatus.PROCESSING
                        elif data['o']['X'] == ClientsOrderStatuses.FILLED and data['o']['m'] is False:
                            status = OrderStatus.FULLY_EXECUTED
                        elif data['o']['X'] == ClientsOrderStatuses.FILLED and data['o']['m'] is True:
                            status = OrderStatus.FULLY_EXECUTED
                        elif data['o']['X'] == ClientsOrderStatuses.PARTIALLY_FILLED:
                            status = OrderStatus.PARTIALLY_EXECUTED
                        elif data['o']['X'] == ClientsOrderStatuses.CANCELED and data['o']['z'] == '0':
                            status = OrderStatus.NOT_EXECUTED

                        if status:
                            result = {
                                'exchange_order_id': data['o']['i'],
                                'exchange': self.EXCHANGE_NAME,
                                'status': status,
                                'factual_price': 0 if status == OrderStatus.PROCESSING else float(data['o']['ap']),
                                'factual_amount_coin': 0 if status == OrderStatus.PROCESSING else float(data['o']['z']),
                                'factual_amount_usd': 0 if status == OrderStatus.PROCESSING else float(
                                    data['o']['z']) * float(data['o']['ap']),
                                'datetime_update': datetime.datetime.utcnow(),
                                'ts_update': time.time() * 1000
                            }

                            if self.symbol == data['o']['s'] and result['status'] != OrderStatus.PROCESSING:
                                self.orders.update({data['o']['i']: result})


if __name__ == '__main__':
    client = BinanceClient(Config.BINANCE, Config.LEVERAGE)
    client.run_updater()

    # async def funding():
    #     async with aiohttp.ClientSession() as session:
    #         await client.get_orderbook_by_symbol(client.symbol)
    #
    # asyncio.run(funding())

    while True:
        time.sleep(1)
