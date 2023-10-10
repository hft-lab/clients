import asyncio
import base64
import hashlib
import hmac

import threading
import time
import traceback
import uuid
from datetime import datetime

from urllib.parse import urlencode

import aiohttp
import orjson
import requests

from core.base_client import BaseClient
from clients.enums import ConnectMethodEnum, ResponseStatus, OrderStatus, PositionSideEnum
import telebot


class KrakenClient(BaseClient):
    BASE_WS = 'wss://futures.kraken.com/ws/v1'
    BASE_URL = 'https://futures.kraken.com'
    EXCHANGE_NAME = 'KRAKEN'
    LAST_ORDER_ID = None
    urlOrderbooks = "https://futures.kraken.com/derivatives/api/v3/orderbook?symbol="
    urlMarkets = "https://futures.kraken.com/derivatives/api/v3/tickers"

    def __init__(self, keys, leverage, alert_id, alert_token):
        self.chat_id = int(alert_id)
        self.telegram_bot = telebot.TeleBot(alert_token)
        self.amount = None
        self.taker_fee = 0.0005
        self.requestLimit = 1200
        self.markets = {}
        self.headers = {"Content-Type": "application/json"}
        self.leverage = leverage
        self.symbol = keys['SYMBOL']
        self.__api_key = keys['API_KEY']
        self.__secret_key = keys['API_SECRET']
        self.__last_challenge = None
        self.orders = {}
        self.tickers = None
        self.count_flag = False
        self.error_info = None
        self.balance = {
            'total': 0.0,
            'timestamp': time.time()
        }
        self.last_price = {
            'sell': 0,
            'buy': 0
        }
        self.max_bid = 0
        self.min_ask = 10000000
        self.positions = {
            self.symbol: {
                'amount': 0,
                'entry_price': 0,
                'unrealized_pnl_usd': 0,
                'side': 'LONG',
                'amount_usd': 0,
                'realized_pnl_usd': 0}
        }
        self.get_balance()
        self.orderbook = {self.symbol: {'sell': {}, 'buy': {}, 'timestamp': 0}}
        self.pings = []
        self.price = 0
        self.amount = 0
        self.instruments = self.get_instruments()

    def get_instruments(self):
        url_path = "/derivatives/api/v3/instruments"
        res = requests.get(url=self.BASE_URL + url_path).json()
        # for instrument in res['instruments']:
        #     if instrument['symbol'] == 'pf_seiusd':
        #         print(instrument)
        return res['instruments']

    def get_sizes(self, symbol):
        for ticker in self.tickers:
            if ticker.get('symbol'):
                if ticker['symbol'] == symbol:
                    values = [ticker.get('askSize', None),
                              ticker.get('vol24h', None),
                              ticker.get('lastSize', None),
                              ticker.get('bidSize', None)]

        max_value = 0
        for value in values:
            if not value:
                continue
            splt = str(value).split('.')[1] if '.' in str(value) else ''
            if len(splt) >= max_value:
                max_value = max(len(splt), max_value)
        if max_value:
            step_size = float('0.' + '0' * (max_value - 1) + str(1))
        else:
            step_size = 1
        for instrument in self.instruments:
            if symbol == instrument['symbol']:
                tick_size = instrument['tickSize']
                break
        if '.' in str(step_size):
            quantity_precision = len(str(step_size).split('.')[1])
        else:
            quantity_precision = 0
        if '.' in str(tick_size):
            price_precision = len(str(tick_size).split('.')[1])
        elif 'e' in str(tick_size):
            price_precision = int((str(tick_size).split('-')[1])) - 1
        else:
            price_precision = 0
        return price_precision, quantity_precision, tick_size, step_size

    def get_available_balance(self, side: str) -> float:
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

    def cancel_all_orders(self, orderID=None) -> dict:
        return self.__cancel_open_orders()

    def get_positions(self) -> dict:
        return self.positions

    def get_markets(self):
        markets = requests.get(url=self.urlMarkets, headers=self.headers).json()
        self.tickers = markets['tickers']
        for market in markets['tickers']:
            if market.get('tag') is not None:
                if (market['tag'] == 'perpetual') & (market['pair'].split(":")[1] == 'USD'):
                    if market['postOnly']:
                        message = f"{self.EXCHANGE_NAME}:\n{market['symbol']} has status PostOnly"
                        try:
                            self.telegram_bot.send_message(self.chat_id, '<pre>' + message + '</pre>', parse_mode='HTML')
                        except:
                            pass
                        continue
                    coin = market['pair'].split(":")[0]
                    self.markets.update({coin: market['symbol']})
        return self.markets

    def get_real_balance(self) -> float:
        if time.time() - self.balance['timestamp'] > 10:
            self.get_balance()
        return self.balance['total']

    def get_orderbook(self) -> dict:
        orderbook = {}
        # time_start = time.time()
        while True:
            snap = self.orderbook[self.symbol]
            orderbook[self.symbol] = {'timestamp': self.orderbook[self.symbol]['timestamp']}
            try:
                orderbook[self.symbol]['asks'] = [[x, snap['sell'][x]] for x in sorted(snap['sell'])]
                orderbook[self.symbol]['bids'] = [[x, snap['buy'][x]] for x in sorted(snap['buy'])][::-1]
            except:
                continue
            # print(f"Orderbook fetch time: {time.time() - time_start}")
            return orderbook

    async def get_multi_orderbook(self, symbol):
        async with aiohttp.ClientSession() as session:
            async with session.get(url=self.urlOrderbooks + symbol) as response:
                full_response = await response.json()
                ob = full_response['orderBook']
                ts_exchange = int(
                    datetime.strptime(full_response['serverTime'], "%Y-%m-%dT%H:%M:%S.%fZ").timestamp() * 1000)
                return {'top_bid': ob['bids'][0][0], 'top_ask': ob['asks'][0][0],
                        'bid_vol': ob['bids'][0][1], 'ask_vol': ob['asks'][0][1],
                        'ts_exchange': ts_exchange}

    def get_last_price(self, side: str) -> float:
        return self.last_price[side.lower()]

    def run_updater(self) -> None:
        _loop_public = asyncio.new_event_loop()
        _loop_private = asyncio.new_event_loop()
        wsd_public = threading.Thread(target=self._run_forever, args=[ConnectMethodEnum.PUBLIC, _loop_public])
        bal_check = threading.Thread(target=self._run_forever, args=[ConnectMethodEnum.PRIVATE, _loop_private])
        wsd_public.start()
        bal_check.start()
        time.sleep(5)

    def _run_forever(self, type, loop) -> None:
        loop.run_until_complete(self._run_loop(type))

    async def _run_loop(self, type) -> None:
        async with aiohttp.ClientSession() as session:
            if type == ConnectMethodEnum.PUBLIC:
                await self._symbol_data_getter(session)
            elif type == ConnectMethodEnum.PRIVATE:
                await self._user_balance_getter(session)

    # PUBLIC -----------------------------------------------------------------------------------------------------------
    async def get_orderbook_by_symbol(self, symbol=None):
        async with aiohttp.ClientSession() as session:
            url_path = "/derivatives/api/v3/orderbook"
            async with session.get(
                    url=self.BASE_URL + url_path + f'?symbol={symbol if symbol else self.symbol}') as resp:
                res = await resp.json()
                orderbook = {}
                orderbook.update({'bids': res['orderBook']['bids']})
                orderbook.update({'asks': res['orderBook']['asks']})
                orderbook.update({'timestamp': int(time.time() * 1000)})
            await session.close()
        return orderbook

    def get_orderbook_by_symbol_non_async(self, symbol=None):
        url_path = "/derivatives/api/v3/orderbook"
        resp = requests.get(url=self.BASE_URL + url_path + f'?symbol={symbol if symbol else self.symbol}').json()
        orderbook = {}
        orderbook.update({'bids': resp['orderBook']['bids']})
        orderbook.update({'asks': resp['orderBook']['asks']})
        orderbook.update({'timestamp': int(time.time() * 1000)})
        return orderbook

    async def _symbol_data_getter(self, session: aiohttp.ClientSession) -> None:
        # await self.get_orderbook_by_symbol()

        async with session.ws_connect(self.BASE_WS) as ws:
            await ws.send_str(orjson.dumps({
                "event": "subscribe",
                "feed": "book",
                'snapshot': False,
                "product_ids": [
                    self.symbol.upper(),
                ]
            }).decode('utf-8'))

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    payload = orjson.loads(msg.data)
                    if not payload.get('event'):
                        if payload.get('feed') == 'book_snapshot':
                            self.orderbook[self.symbol] = {
                                'sell': {x['price']: x['qty'] for x in payload['asks']},
                                'buy': {x['price']: x['qty'] for x in payload['bids']},
                                'timestamp': payload['timestamp']
                            }
                            self.max_bid = max(self.orderbook[self.symbol]['buy'].keys())
                            self.min_ask = min(self.orderbook[self.symbol]['sell'].keys())
                            self.count_flag = True
                        elif payload.get('feed') == 'book':
                            res = self.orderbook[self.symbol][payload['side']]
                            if res.get(payload['price']) and not payload['qty']:
                                del res[payload['price']]
                            else:
                                self.orderbook[self.symbol][payload['side']][payload['price']] = payload['qty']
                                self.orderbook[self.symbol]['timestamp'] = payload['timestamp']
                            if payload['side'] == 'sell':
                                if payload['price'] < self.min_ask:
                                    self.min_ask = payload['price']
                                    self.count_flag = True
                            else:
                                if payload['price'] > self.max_bid:
                                    self.max_bid = payload['price']
                                    self.count_flag = True

    # PRIVATE ----------------------------------------------------------------------------------------------------------

    async def get_fills(self, session) -> dict:
        url_path = '/derivatives/api/v3/fills'
        nonce = str(int(time.time() * 1000))
        params = {
            "lastFillTime": str(round((time.time() - 600) * 1000)),
        }

        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "Nonce": nonce,
            "APIKey": self.__api_key,
            "Authent": self.get_kraken_futures_signature(
                url_path, '', nonce
            ).decode('utf-8')
        }
        async with session.get(headers=headers, url=self.BASE_URL + url_path, data=params) as resp:
            return await resp.json()

    async def get_all_orders(self, symbol, session) -> list:
        all_orders = await self.__get_all_orders(session)
        res_orders = {}

        for elements in all_orders['elements']:
            for event in ['OrderPlaced', 'OrderCancelled']:
                # print(elements)
                if order := elements['event'].get(event, {}).get('order'):
                    if order['clientId'] == "":
                        if int(order['lastUpdateTimestamp']) > int(time.time() * 1000) - 86400000:
                            amount = float(order['quantity']) if order['filled'] == '0' else float(
                                order['filled']) - float(order['quantity'])
                            status = OrderStatus.NOT_EXECUTED if not float(
                                order['quantity']) else OrderStatus.FULLY_EXECUTED
                            res_orders.update({order['uid']: {
                                'id': uuid.uuid4(),
                                'datetime': datetime.fromtimestamp(int(order['lastUpdateTimestamp'] / 1000)),
                                'ts': int(order['lastUpdateTimestamp']),
                                'context': 'web-interface',
                                'parent_id': uuid.uuid4(),
                                'exchange_order_id': order['uid'],
                                'type': 'GTC',  # TODO check it
                                'status': status,
                                'exchange': self.EXCHANGE_NAME,
                                'side': order['direction'].lower(),
                                'symbol': order['tradeable'],
                                'expect_price': float(order['limitPrice']),
                                'expect_amount_coin': amount,
                                'expect_amount_usd': float(order['limitPrice']) * amount,
                                'expect_fee': self.taker_fee,
                                'factual_price': float(order['limitPrice']),
                                'factual_amount_coin': 0 if order['filled'] == '0' else float(order['filled']) - float(
                                    order['quantity']),
                                'factual_amount_usd': float(order['limitPrice']) * amount,
                                'order_place_time': 0,
                                'factual_fee': self.taker_fee,
                                'env': '-',
                                'datetime_update': datetime.utcnow(),
                                'ts_update': int(time.time() * 1000),
                                'client_id': order['clientId']
                            }})

        return [x for x in res_orders.values()]

    async def __get_all_orders(self, session) -> dict:
        url_path = '/api/history/v3/orders'
        nonce = str(int(time.time() * 1000))

        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "Nonce": nonce,
            "APIKey": self.__api_key,
            "Authent": self.get_kraken_futures_signature(
                url_path, '', nonce
            ).decode('utf-8')
        }
        async with session.get(headers=headers, url=self.BASE_URL + url_path) as resp:
            return await resp.json()

    async def get_order_by_id(self, symbol, order_id: str, session: aiohttp.ClientSession) -> dict:
        fills_orders = await self.get_fills(session)
        if fills_orders.get('result') == 'success':
            prices = []
            sizes = []
            for fill in fills_orders.get('fills', []):
                if fill['order_id'] == order_id:
                    if fill['fillType'] == 'taker':
                        return {
                            'exchange_order_id': order_id,
                            'exchange': self.EXCHANGE_NAME,
                            'status': OrderStatus.FULLY_EXECUTED,
                            'factual_price': fill['price'],
                            'factual_amount_coin': fill['size'],
                            'factual_amount_usd': fill['size'] * fill['price'],
                            'datetime_update': datetime.utcnow(),
                            'ts_update': int(time.time() * 1000)
                        }
                    elif fill['fillType'] == 'maker':
                        prices.append(fill['price'])
                        sizes.append(fill['size'])

            if sizes and prices:
                sizes = sum(sizes)
                price = sum(prices) / len(prices)
                return {
                    'exchange_order_id': order_id,
                    'exchange': self.EXCHANGE_NAME,
                    'status': OrderStatus.FULLY_EXECUTED,
                    'factual_price': price,
                    'factual_amount_coin': sizes,
                    'factual_amount_usd': sizes * price,
                    'datetime_update': datetime.utcnow(),
                    'ts_update': int(time.time() * 1000)
                }
        return await self.__get_order_by_all_orders(session, order_id)

    async def __get_order_by_all_orders(self, session, order_id):
        all_orders = await self.__get_all_orders(session)
        if orders := all_orders.get('elements'):
            for order in orders:
                stts = OrderStatus.PROCESSING
                price = 0
                amount = 0
                if last_update := order['event'].get('OrderUpdated'):
                    if last_update['newOrder']['uid'] == order_id:
                        # print(f"KRAKEN {order=}")
                        rsn = last_update['reason']
                        if rsn in ['full_fill', 'partial_fill']:
                            stts = OrderStatus.FULLY_EXECUTED if rsn == 'full_fill' else OrderStatus.PARTIALLY_EXECUTED
                            price = last_update['newOrder']['limitPrice']
                            amount = last_update['newOrder']['filled']
                        elif rsn in ['cancelled_by_user', 'cancelled_by_admin', 'not_enough_margin']:
                            stts = OrderStatus.NOT_EXECUTED
                        return {
                            'exchange_order_id': last_update['oldOrder']['uid'],
                            'exchange': self.EXCHANGE_NAME,
                            'status': stts,
                            'factual_price': price,
                            'factual_amount_coin': amount,
                            'factual_amount_usd': amount * price,
                            'datetime_update': datetime.utcnow(),
                            'ts_update': int(time.time() * 1000)
                        }
            return {
                'exchange_order_id': order_id,
                'exchange': self.EXCHANGE_NAME,
                'status': OrderStatus.NOT_EXECUTED,
                'factual_price': 0,
                'factual_amount_coin': 0,
                'factual_amount_usd': 0,
                'datetime_update': datetime.utcnow(),
                'ts_update': int(time.time() * 1000)
            }

    def _sign_message(self, api_path: str, data: dict) -> str:
        plain_data = []
        for key, value in data.items():
            if isinstance(value, (list, tuple)):
                plain_data.extend([(key, str(item)) for item in value])
            else:
                plain_data.append((key, str(value)))

        post_data = urlencode(plain_data)
        encoded = (str(data["nonce"]) + post_data).encode()
        message = api_path.encode() + hashlib.sha256(encoded).digest()
        signature = hmac.new(base64.b64decode(self.__secret_key), message, hashlib.sha512)
        sig_digest = base64.b64encode(signature.digest())

        return sig_digest.decode()

    def get_kraken_futures_signature(self, endpoint: str, data: str, nonce: str):
        if endpoint.startswith("/derivatives"):
            endpoint = endpoint[len("/derivatives"):]

        sha256_hash = hashlib.sha256()
        sha256_hash.update((data + nonce + endpoint).encode("utf8"))

        return base64.b64encode(
            hmac.new(
                base64.b64decode(self.__secret_key), sha256_hash.digest(), hashlib.sha512
            ).digest()
        )

    def __cancel_open_orders(self):
        url_path = "/derivatives/api/v3/cancelallorders"
        nonce = str(int(time.time() * 1000))
        params = {'symbol': self.symbol}
        post_string = "&".join([f"{key}={params[key]}" for key in sorted(params)])
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "Nonce": nonce,
            "APIKey": self.__api_key,
            "Authent": self.get_kraken_futures_signature(
                url_path, post_string, nonce
            ),
        }
        return requests.post(headers=headers, url=self.BASE_URL + url_path, data=post_string).json()

    def get_balance(self):
        url_path = "/derivatives/api/v3/accounts"
        nonce = str(int(time.time() * 1000))

        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "Nonce": nonce,
            "APIKey": self.__api_key,
            "Authent": self.get_kraken_futures_signature(
                url_path, '', nonce
            ),
        }
        res = requests.get(headers=headers, url=self.BASE_URL + url_path).json()
        self.balance['total'] = res['accounts']['flex']['portfolioValue']
        self.balance['free'] = res['accounts']['flex']['availableMargin']
        self.balance['timestamp'] = time.time()

    def fit_sizes(self, amount, price, symbol) -> None:
        price_precision, quantity_precision, tick_size, step_size = self.get_sizes(symbol)
        self.amount = round(amount - (amount % step_size), quantity_precision)
        self.price = round(price - (price % tick_size), price_precision)


    async def create_order(self, symbol, side: str, session: aiohttp.ClientSession, expire=5000, client_id=None):
        time_sent = datetime.utcnow().timestamp()
        nonce = str(int(time.time() * 1000))
        url_path = "/derivatives/api/v3/sendorder"
        params = {
            "orderType": "lmt",  # post
            "limitPrice": self.price,
            "side": side.lower(),
            "size": self.amount,
            "symbol": symbol,
            "cliOrdId": client_id
        }
        post_string = "&".join([f"{key}={params[key]}" for key in sorted(params)])
        print(f"KRAKEN BODY: {params}")
        headers = {
            "Content-Type": "application/json",
            "Nonce": nonce,
            "APIKey": self.__api_key,
            "Authent": self.get_kraken_futures_signature(url_path, post_string, nonce).decode('utf-8'),
        }
        url = self.BASE_URL + url_path + '?' + post_string
        async with session.post(url=url, headers=headers, data=post_string) as resp:
            response = await resp.json()
            print(f'KRAKEN RESPONSE: {response}')
            self.LAST_ORDER_ID = response.get('sendStatus', {}).get('order_id', 'default')
            if response['sendStatus'].get('receivedTime'):
                timestamp = response['sendStatus']['receivedTime']
                timestamp = int(datetime.timestamp(datetime.strptime(timestamp, '%Y-%m-%dT%H:%M:%S.%fZ')) * 1000)
                status = ResponseStatus.SUCCESS
            else:
                timestamp = 0000000000000
                status = ResponseStatus.ERROR
                self.error_info = response
            ping = int(round(timestamp - (time_sent * 1000), 0))
            self.pings.append(ping)
            with open(f'{self.EXCHANGE_NAME}_pings.txt', 'a') as file:
                file.write(str(datetime.utcnow()) + ' ' + str(ping) + '\n')
            avr = int(round((sum(self.pings) / len(self.pings)), 0))
            print(f"{self.EXCHANGE_NAME}: ping {ping}|avr: {avr}|max: {max(self.pings)}|min: {min(self.pings)}")
            return {
                'exchange_name': self.EXCHANGE_NAME,
                'timestamp': timestamp,
                'status': status
            }

    # NEW FUNCTIONS
    def get_position(self):
        nonce = str(int(time.time() * 1000))
        url_path = '/derivatives/api/v3/openpositions'
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "Nonce": nonce,
            "APIKey": self.__api_key,
            "Authent": self.get_kraken_futures_signature(url_path, '', nonce).decode('utf-8'),
        }
        res = requests.get((self.BASE_URL + url_path), headers=headers).json()
        if isinstance(res, dict):
            for payload in res.get('openPositions', []):
                if float(payload['size']):
                    side = PositionSideEnum.LONG if payload['side'] == 'long' else PositionSideEnum.SHORT
                    amount_usd = payload['size'] * payload['price']
                    self.positions.update({payload['symbol'].upper(): {
                        'side': side,
                        'amount_usd': amount_usd if side == PositionSideEnum.LONG else -amount_usd,
                        'amount': payload['size'] if side == PositionSideEnum.LONG else -payload['size'],
                        'entry_price': payload['price'],
                        'unrealized_pnl_usd': 0,
                        'realized_pnl_usd': 0,
                        'lever': self.leverage
                    }})

    def _get_sign_challenge(self, challenge: str) -> str:
        sha256_hash = hashlib.sha256()
        sha256_hash.update(challenge.encode("utf-8"))
        return base64.b64encode(
            hmac.new(
                base64.b64decode(self.__secret_key), sha256_hash.digest(), hashlib.sha512
            ).digest()
        ).decode("utf-8")

    async def _user_balance_getter(self, session: aiohttp.ClientSession) -> None:
        async with session.ws_connect(self.BASE_WS) as ws:
            if not self.__last_challenge:
                await ws.send_str(orjson.dumps({"event": "challenge", "api_key": self.__api_key}).decode('utf-8'))

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        msg_data = orjson.loads(msg.data)
                        if msg_data.get('event') == 'challenge':
                            self.__last_challenge = msg_data['message']
                            await ws.send_str(orjson.dumps({
                                "event": "subscribe",
                                "feed": "balances",
                                "api_key": self.__api_key,
                                'original_challenge': self.__last_challenge,
                                "signed_challenge": self._get_sign_challenge(self.__last_challenge)
                            }).decode('utf-8'))

                            await ws.send_str(orjson.dumps({
                                "event": "subscribe",
                                "feed": "fills",
                                "api_key": self.__api_key,
                                'original_challenge': self.__last_challenge,
                                "signed_challenge": self._get_sign_challenge(self.__last_challenge)
                            }).decode('utf-8'))

                            await ws.send_str(orjson.dumps({
                                "event": "subscribe",
                                "feed": "open_positions",
                                "api_key": self.__api_key,
                                'original_challenge': self.__last_challenge,
                                "signed_challenge": self._get_sign_challenge(self.__last_challenge)
                            }).decode('utf-8'))

                        elif msg_data.get('feed') in ['balances']:
                            if msg_data.get('flex_futures'):
                                self.balance['timestamp'] = time.time()
                                self.balance['total'] = msg_data['flex_futures']['portfolio_value']
                                self.balance['free'] = msg_data['flex_futures']['available_margin']

                        elif msg_data.get('feed') == 'open_positions':
                            for position in msg_data.get('positions', []):
                                side = PositionSideEnum.LONG if position['balance'] >= 0 else PositionSideEnum.SHORT
                                amount_usd = position['balance'] * position['mark_price']
                                self.positions.update({position['instrument'].upper(): {
                                    'side': side,
                                    'amount_usd': amount_usd,
                                    'amount': position['balance'],
                                    'entry_price': position['entry_price'],
                                    'unrealized_pnl_usd': 0,
                                    'realized_pnl_usd': position['pnl'],
                                    'lever': self.leverage
                                }})
                        elif msg_data.get('fills') and msg_data['feed'] != 'fills_snapshot':
                            for fill in msg_data['fills']:
                                print(fill)
                                qty_coin = fill['qty'] - fill['remaining_order_qty']
                                qty_usd = round(qty_coin * fill['price'], 1)
                                status = OrderStatus.PARTIALLY_EXECUTED
                                if fill['remaining_order_qty'] == 0:
                                    status = OrderStatus.FULLY_EXECUTED
                                if order := self.orders.get(fill['order_id']):
                                    qty_usd = order['factual_amount_usd'] + qty_usd
                                    factual_price = qty_usd / qty_coin
                                    self.last_price['buy' if fill['buy'] else 'sell'] = factual_price
                                    self.orders.update({fill['order_id']: {
                                        'factual_price': factual_price,
                                        'exchange_order_id': fill['order_id'],
                                        'exchange': self.EXCHANGE_NAME,
                                        'status': status,
                                        'factual_amount_coin': qty_coin,
                                        'factual_amount_usd': qty_usd,
                                        'datetime_update': datetime.utcnow(),
                                        'ts_update': int(time.time() * 1000)
                                    }})
                                    # print(self.orders)
                                else:
                                    self.last_price['buy' if fill['buy'] else 'sell'] = fill['price']
                                    self.orders.update({fill['order_id']: {
                                        'factual_price': fill['price'],
                                        'exchange_order_id': fill['order_id'],
                                        'exchange': self.EXCHANGE_NAME,
                                        'status': status,
                                        'factual_amount_coin': qty_coin,
                                        'factual_amount_usd': qty_usd,
                                        'datetime_update': datetime.utcnow(),
                                        'ts_update': int(time.time() * 1000)
                                    }})
                                    # print(self.orders)

                                # self.last_price['sell' if order['direction'] else 'buy'] = float(order['limit_price'])
                                # status = OrderStatus.PROCESSING
                                #
                                # if msg_data['reason'] == 'full_fill':
                                #     status = OrderStatus.FULLY_EXECUTED
                                # elif msg_data['reason'] == 'partial_fill':
                                #     status = OrderStatus.PARTIALLY_EXECUTED
                                # elif msg_data['reason'] in ['cancelled_by_user', 'cancelled_by_admin', 'not_enough_margin']:
                                #     status = OrderStatus.NOT_EXECUTED
                                #
                                # if status:
                                #     price = 0 if status in [OrderStatus.PROCESSING, OrderStatus.NOT_EXECUTED] \
                                #         else order['limit_price']
                                #     amount = 0 if status in [OrderStatus.PROCESSING, OrderStatus.NOT_EXECUTED] \
                                #         else order['filled']
                                #     result = {
                                #         'exchange_order_id': order['order_id'],
                                #         'exchange': self.EXCHANGE_NAME,
                                #         'status': status,
                                #         'factual_price': price,
                                #         'factual_amount_coin': amount,
                                #         'factual_amount_usd': 0 if status in [OrderStatus.PROCESSING,
                                #                                               OrderStatus.NOT_EXECUTED] \
                                #             else amount * price,
                                #         'datetime_update': datetime.utcnow(),
                                #         'ts_update': time.time() * 1000
                                #     }
                                #
                                #     if self.symbol.upper() == order['instrument'].upper() \
                                #             and result['status'] != OrderStatus.PROCESSING:
                                #         self.orders.update({order['order_id']: result})


if __name__ == '__main__':
    import configparser
    import sys

    config = configparser.ConfigParser()
    config.read(sys.argv[1], "utf-8")
    client = KrakenClient(config['KRAKEN'],
                          config['SETTINGS']['LEVERAGE'],
                          config['TELEGRAM']['ALERT_CHAT_ID'],
                          config['TELEGRAM']['ALERT_BOT_TOKEN'])
    client.run_updater()
    print(client.get_markets())
    # time.sleep(5)

    # async def test_order():
    #     async with aiohttp.ClientSession() as session:
    #         ob = await client.get_multi_orderbook('pf_runeusd')
    #         price = ob['top_ask']
    #         client.get_markets()
    #         client.fit_sizes(30, price, 'pf_runeusd')
    #         data = await client.create_order('pf_runeusd',
    #                                          'buy',
    #                                          session,
    #                                          client_id=f"api_deal_{str(uuid.uuid4()).replace('-', '')[:20]}")
    #         # data = client.get_balance()
    #         print(data)
    #         print()
    #         print()
    #         print()
    #         # client.cancel_all_orders()
    # #
    # #
    # time.sleep(5)
    # asyncio.run(test_order())
    # while True:
    #     time.sleep(5)
    #
