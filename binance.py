import asyncio
import datetime
import hashlib
import hmac
import threading
import time
import traceback
from pprint import pprint
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

        self._get_listen_key()
        self._get_position()

    def get_available_balance(self, side: str) -> float:
        return self.__get_available_balance(side)

    async def create_order(self, amount, price, side, session: aiohttp.ClientSession,
                           expire: int = 100, client_ID: str = None) -> dict:
        return await self.__create_order(amount, price, side.upper(), session, expire, client_ID)

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

    async def _symbol_data_getter(self, session: aiohttp.ClientSession) -> None:
        async with session.ws_connect(self.BASE_WS + self.symbol.lower()) as ws:
            await ws.send_str(orjson.dumps({
                'id': 1,
                'method': 'SUBSCRIBE',
                'params': [f"{self.symbol.lower()}@depth5@100ms"]
            }).decode('utf-8'))

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    payload = orjson.loads(msg.data)
                    if payload.get('a'):
                        self.orderbook.update(
                            {
                                self.symbol: {
                                    'asks': [[float(x) for x in j] for j in payload['a']],
                                    'bids': [[float(x) for x in j] for j in reversed(payload['b'])],
                                    'timestamp': payload['E']
                                }
                            }
                        )

    # PRIVATE ----------------------------------------------------------------------------------------------------------
    @staticmethod
    def _prepare_query(params: dict) -> str:
        return urlencode(params)

    def __get_available_balance(self, side):
        position_value = 0
        for market, position in self.positions.items():
            if position.get('amount'):  # and market.upper() == self.symbol.upper():
                position_value += position['amount_usd']

        available_margin = self.balance['total'] * self.leverage

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

    async def get_funding_history(self, session):
        url_path = "/fapi/v1/fundingRate"
        payload = {
            "timestamp": int(time.time() * 1000),
            'symbol': self.symbol
        }

        query_string = self._prepare_query(payload)
        payload["signature"] = self._create_signature(query_string)
        query_string = self._prepare_query(payload)

        async with session.get(url=self.BASE_URL + url_path + '?' + query_string, headers=self.headers) as resp:
            pprint(await resp.json())

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

    async def __create_order(self, amount: float, price: float, side: str, session: aiohttp.ClientSession,
                             expire=5000, client_ID=None) -> dict:
        self.expect_amount_coin = float(round(float(round(amount / self.step_size) * self.step_size), self.quantity_precision))
        self.expect_price = float(round(float(round(price / self.tick_size) * self.tick_size), self.price_precision))

        url_path = "https://fapi.binance.com/fapi/v1/order?"
        query_string = f"timestamp={int(time.time() * 1000)}&symbol={self.symbol}&side={side}&type=LIMIT&" \
                       f"price={self.expect_price}&quantity={self.expect_amount_coin}&timeInForce=GTC&recvWindow=5000"
        query_string += f'&signature={self._create_signature(query_string)}'

        async with session.post(url=url_path + query_string, headers=self.headers) as resp:
            res = await resp.json()
            print(f'BINANCE RESPONSE: {res}')
            timestamp = 0000000000000
            if res.get('code') and -5023 < res['code'] < -1099:
                status = ResponseStatus.ERROR
            elif res.get('status'):
                status = ResponseStatus.SUCCESS
                self.LAST_ORDER_ID = res['orderId']
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

    async def get_order_by_id(self, order_id: str, session: aiohttp.ClientSession):
        url_path = "/fapi/v1/order"
        payload = {
            "timestamp": int(time.time() * 1000),
            "symbol": self.symbol,
            "orderId": int(order_id),
            "recvWindow": int((time.time() + 2) * 1000)
        }

        query_string = self._prepare_query(payload)
        payload["signature"] = self._create_signature(query_string)
        query_string = self._prepare_query(payload)

        async with session.get(url=self.BASE_URL + url_path + "?" + query_string, headers=self.headers) as resp:
            res = await resp.json()

            if res.get('status') == ClientsOrderStatuses.FILLED and res.get('time', 0) == res.get('updateTime'):
                status = OrderStatus.INSTANT_FULLY_EXECUTED
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
                'factual_amount_usd': float(res['executedQty']) * float(res['avgPrice'])
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

                        self.last_price[data['o']['S'].lower()] = float(data['o']['ap'])

                        if data['o']['X'] == ClientsOrderStatuses.NEW:
                            status = OrderStatus.PROCESSING
                        elif data['o']['X'] == ClientsOrderStatuses.FILLED and data['o']['m'] is False:
                            status = OrderStatus.INSTANT_FULLY_EXECUTED
                        elif data['o']['X'] == ClientsOrderStatuses.FILLED and data['o']['m'] is True:
                            status = OrderStatus.DELAYED_FULLY_EXECUTED
                        elif data['o']['X'] == ClientsOrderStatuses.PARTIALLY_FILLED:
                            status = OrderStatus.PARTIALLY_EXECUTED
                        else:
                            status = OrderStatus.NOT_EXECUTED

                        result = {
                            'exchange_order_id': data['o']['i'],
                            'exchange': self.EXCHANGE_NAME,
                            'status': status,
                            'factual_price': 0 if status == OrderStatus.PROCESSING else float(data['o']['ap']),
                            'factual_amount_coin': 0 if status == OrderStatus.PROCESSING else float(data['o']['z']),
                            'factual_amount_usd': 0 if status == OrderStatus.PROCESSING else float(data['o']['z']) * float(data['o']['ap']),
                            'datetime_update': datetime.datetime.utcnow(),
                            'ts_update': time.time() * 1000
                        }

                        if self.symbol == data['o']['s'] and result['status'] != OrderStatus.PROCESSING:
                            self.orders.update({data['o']['i']: result})



if __name__ == '__main__':
    client = BinanceClient(Config.BINANCE, Config.LEVERAGE)


    async def f():
        async with aiohttp.ClientSession() as s:
            await client.get_order_by_id('8389765602994765202', s)

    asyncio.run(f())

