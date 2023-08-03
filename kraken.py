import asyncio
import base64
import hashlib
import hmac

import os
import threading
import time
import uuid
from datetime import datetime
from pprint import pprint

from urllib.parse import urlencode

import aiohttp
import orjson
import requests

from config import Config
from core.base_client import BaseClient
from clients.enums import ConnectMethodEnum, ResponseStatus, OrderStatus, PositionSideEnum


class KrakenClient(BaseClient):
    BASE_WS = 'wss://futures.kraken.com/ws/v1'
    BASE_URL = 'https://futures.kraken.com'
    EXCHANGE_NAME = 'KRAKEN'
    LAST_ORDER_ID = None

    def __init__(self, keys, leverage):
        self.expect_amount_coin = None
        self.taker_fee = 0.0005
        self.leverage = leverage
        self.symbol = keys['symbol']
        self.__api_key = keys['api_key']
        self.__secret_key = keys['secret_key']
        self.__last_challenge = None
        self.orders = {}
        self.step_size = 0
        self.tick_size = None
        self.price_precision = 0
        self.quantity_precision = 0
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
        self._loop_public = asyncio.new_event_loop()
        self._loop_private = asyncio.new_event_loop()
        self.wsd_public = threading.Thread(target=self._run_forever,
                                           args=[ConnectMethodEnum.PUBLIC, self._loop_public])
        self.bal_check = threading.Thread(target=self._run_forever,
                                          args=[ConnectMethodEnum.PRIVATE, self._loop_private])
        self.orderbook = {self.symbol: {'sell': {}, 'buy': {}, 'timestamp': 0}}
        self.pings = []

    def get_sizes(self):
        asks_value = [str(x[1]) for x in self.get_orderbook()[self.symbol]['asks']]
        max_value = 0
        for v in asks_value:
            splt = v.split('.')[1]

            if len(splt) >= max_value:
                max_value = max(len(splt), max_value)

        self.step_size = float('0.' + '0' * (max_value - 1) + str(1))

        url_path = "/derivatives/api/v3/instruments"
        res = requests.get(url=self.BASE_URL + url_path).json()

        for instrument in res['instruments']:
            if self.symbol == instrument['symbol'].upper():
                self.tick_size = instrument['tickSize']
                break

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

    async def create_order(self, price, side, session: aiohttp.ClientSession,
                           expire: int = 5000, client_id: str = None) -> dict:
        return await self.__create_order(price, side.upper(), session, expire, client_id)

    def cancel_all_orders(self, orderID=None) -> dict:
        return self.__cancel_open_orders()

    def get_positions(self) -> dict:
        return self.positions

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

    def get_last_price(self, side: str) -> float:
        return self.last_price[side.lower()]

    def run_updater(self) -> None:
        self.wsd_public.start()
        self.bal_check.start()
        time.sleep(5)
        self.get_sizes()

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

        self.get_sizes()

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
        self.balance['total'] = res['accounts']['flex']['balanceValue']
        self.balance['timestamp'] = time.time()

    def fit_amount(self, amount) -> None:
        if not self.quantity_precision:
            if '.' in str(self.step_size):
                self.quantity_precision = len(str(self.step_size).split('.')[1])
            else:
                self.quantity_precision = 0
        self.expect_amount_coin = round(amount - (amount % self.step_size), self.quantity_precision)

    async def __create_order(self, price: float, side: str, session: aiohttp.ClientSession, expire=5000, client_id=None):
        time_sent = datetime.utcnow().timestamp()
        nonce = str(int(time.time() * 1000))
        url_path = "/derivatives/api/v3/sendorder"
        self.expect_price = round(round(price / self.tick_size) * self.tick_size, self.price_precision)
        params = {
            "orderType": "lmt",  # post
            "limitPrice": self.expect_price,
            "side": side.lower(),
            "size": self.expect_amount_coin,
            "symbol": self.symbol,
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
            if response.get('result') == 'success':
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
                                "feed": "open_orders",
                                "api_key": self.__api_key,
                                'original_challenge': self.__last_challenge,
                                "signed_challenge": self._get_sign_challenge(self.__last_challenge)
                            }).decode('utf-8'))

                        elif msg_data.get('feed') in ['balances']:
                            if msg_data.get('flex_futures'):
                                self.balance['timestamp'] = time.time()
                                self.balance['total'] = msg_data['flex_futures']['balance_value']

                        elif msg_data.get('feed') == 'open_positions':
                            for position in msg_data.get('positions', []):
                                side = PositionSideEnum.LONG if position['balance'] >= 0 else PositionSideEnum.SHORT
                                amount_usd = position['balance'] * position['mark_price']
                                self.positions.update({position['instrument'].upper(): {
                                    'side': side,
                                    'amount_usd': amount_usd if side == PositionSideEnum.LONG else -amount_usd,
                                    'amount': position['balance'] if side == PositionSideEnum.LONG else -position[
                                        'balance'],
                                    'entry_price': position['entry_price'],
                                    'unrealized_pnl_usd': 0,
                                    'realized_pnl_usd': position['pnl'],
                                    'lever': self.leverage
                                }})
                        elif msg_data.get('fills') and msg_data['feed'] != 'fills_snapshot':
                            for fill in msg_data['fills']:
                                if self.symbol.upper() == fill['instrument'].upper():
                                    qty_coin = fill['qty'] - fill['remaining_order_qty']
                                    qty_usd = round(qty_coin * fill['price'], 1)
                                    status = OrderStatus.PARTIALLY_EXECUTED
                                    if fill['remaining_order_qty'] < self.step_size:
                                        status = OrderStatus.FULLY_EXECUTED
                                    if order := self.orders.get(fill['order_id']):
                                        qty_usd = order['factual_amount_usd'] + qty_usd
                                        factual_price = round(qty_usd / qty_coin, self.price_precision)
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
        client = KrakenClient(Config.KRAKEN, Config.LEVERAGE)
        client.run_updater()
        time.sleep(5)

        async def test_order():
            async with aiohttp.ClientSession() as session:
                client.fit_amount(0.017)
                price = client.get_orderbook()[client.symbol]['bids'][10][0]
                data = await client.create_order(price,
                                                 'buy',
                                                 session,
                                                 client_id=f"api_deal_{str(uuid.uuid4()).replace('-', '')[:20]}")
                # print(data)
                client.cancel_all_orders()


        asyncio.run(test_order())
        while True:
            time.sleep(5)
            asyncio.run(test_order())
    # while True:
    #     client.get_orderbook()
    #     # print(f"ASKS: {client.get_orderbook()[client.symbol]['asks'][:3]}")
    #     # print(f"BIDS: {client.get_orderbook()[client.symbol]['bids'][:3]}\n")
    #     time.sleep(1)
#     #     pprint(client.get_orderbook()[client.symbol]['asks'][:3])
#     #     pprint(client.get_orderbook()[client.symbol]['bids'][:3])
# fill_buy = {"feed": "fills", "username": "4e8b59e8-a716-498c-8dcc-c4835d04f50f", "fills": [
#     {"instrument": "PF_ETHUSD", "time": 1690295256776, "price": 1864.7, "seq": 100, "buy": True, "qty": 0.027,
#      "remaining_order_qty": 0.0, "order_id": "1cb6667b-bd01-46f4-aaf4-34b0ca24b011", "cli_ord_id": "None",
#      "fill_id": "e986e21b-98d1-410d-a8ef-d7a1975df2aa", "fill_type": "taker", "fee_paid": 0.02517345,
#      "fee_currency": "USD", "taker_order_type": "lmt", "order_type": "lmt"}]}