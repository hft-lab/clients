import asyncio
import base64
import hashlib
import hmac

import os
import threading
import time
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
        self.step_size = None
        self.tick_size = None
        self.price_precision = 0
        self.quantity_precision = 0
        self.count_flag = False
        self.error_info = None
        self.old_orderbook = {
            self.symbol: {
                'asks': [[1, 2]],
                'bids': [[1, 2]]
            }
        }
        self.balance = {
            'total': 0.0,
        }
        self.last_price = {
            'sell': 0,
            'buy': 0
        }
        self.positions = {
            self.symbol: {
                'amount': 0,
                'entry_price': 0,
                'unrealized_pnl_usd': 0,
                'side': 'LONG',
                'amount_usd': 0,
                'realized_pnl_usd': 0}
        }
        self.orderbook = {
            self.symbol: {
                'asks': [],
                'bids': [],
                'timestamp': 0
            }
        }
        self.get_balance()
        self._loop_public = asyncio.new_event_loop()
        self._loop_private = asyncio.new_event_loop()
        self.wsd_public = threading.Thread(target=self._run_forever,
                                           args=[ConnectMethodEnum.PUBLIC, self._loop_public])
        self.bal_check = threading.Thread(target=self._run_forever,
                                          args=[ConnectMethodEnum.PRIVATE, self._loop_private])



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
        return self.balance['total']

    def get_orderbook(self) -> dict:
        return self.orderbook

    def get_last_price(self, side: str) -> float:
        return self.last_price[side.lower()]

    def run_updater(self) -> None:
        self.wsd_public.start()
        self.bal_check.start()

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
            async with session.get(url=self.BASE_URL + url_path + f'?symbol={symbol if symbol else self.symbol}') as resp:
                res = await resp.json()
                self.orderbook[symbol if symbol else self.symbol]['bids'] = res['orderBook']['bids']
                self.orderbook[symbol if symbol else self.symbol]['asks'] = res['orderBook']['asks']
                self.orderbook[symbol if symbol else self.symbol]['timestamp'] = int(time.time() * 1000)

    async def _symbol_data_getter(self, session: aiohttp.ClientSession) -> None:
        await self.get_orderbook_by_symbol()

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
                    if payload['event'] == 'subscribed':
                        return
                    if payload.get('feed') == 'book_snapshot':
                        self.orderbook[self.symbol] = {
                            'asks': [[x['price'], x['qty']] for x in payload['asks']],
                            'bids': [[x['price'], x['qty']] for x in payload['bids']],
                            'timestamp': payload['timestamp']
                        }
                        self.count_flag = True
                    elif payload.get('feed') == 'book':
                        last_ob_ask = self.orderbook[self.symbol]['asks'][0][0]
                        last_ob_bid = self.orderbook[self.symbol]['bids'][0][0]
                        index = 0
                        side = 'bids' if payload['side'] == 'buy' else 'asks'
                        new_order = [payload['price'], payload['qty']]
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
                        self.orderbook[self.symbol]['timestamp'] = payload['timestamp']
                        if last_ob_ask != self.orderbook[self.symbol]['asks'][0][0] \
                                or last_ob_bid != self.orderbook[self.symbol]['bids'][0][0]:
                            self.count_flag = True

    # PRIVATE ----------------------------------------------------------------------------------------------------------

    async def get_order_by_id(self, symbol, order_id: str, session: aiohttp.ClientSession) -> dict:
        url_path = f'/api/history/v3/orders'

        nonce = str(int(time.time() * 1000))
        params = {

        }
        post_string = "&".join([f"{key}={params[key]}" for key in sorted(params)])
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "Nonce": nonce,
            "APIKey": self.__api_key,
            "Authent": self.get_kraken_futures_signature(
                url_path, post_string, nonce
            ).decode('utf-8'),
        }
        async with session.get(headers=headers, url=self.BASE_URL + url_path) as resp:
            resp = await resp.json()
            status = OrderStatus.NOT_EXECUTED

            result = {
                'exchange_order_id': order_id,
                'exchange': self.EXCHANGE_NAME,
                'status': status,
                'factual_price': 0,
                'factual_amount_coin': 0,
                'factual_amount_usd': 0,
                'datetime_update': datetime.utcnow(),
                'ts_update': time.time() * 1000
            }

            for order in resp['elements']:

                if last_update := order['event'].get('OrderUpdated'):
                    if last_update['newOrder']['uid'] == order_id:  # noqa
                        pprint(f"{order=}")
                        if last_update['reason'] == 'full_fill':
                            status = OrderStatus.FULLY_EXECUTED
                        elif last_update['reason'] == 'partial_fill':
                            status = OrderStatus.PARTIALLY_EXECUTED
                        elif last_update['reason'] in ['cancelled_by_user', 'cancelled_by_admin', 'not_enough_margin']:
                            status = OrderStatus.NOT_EXECUTED

                        if status:
                            price = float(0 if status in [OrderStatus.PROCESSING, OrderStatus.NOT_EXECUTED] \
                                              else last_update['newOrder']['limitPrice'])
                            amount = float(0 if status in [OrderStatus.PROCESSING, OrderStatus.NOT_EXECUTED] \
                                               else last_update['newOrder']['filled'])

                            result = {
                                'exchange_order_id': last_update['oldOrder']['uid'],
                                'exchange': self.EXCHANGE_NAME,
                                'status': status,
                                'factual_price': price,
                                'factual_amount_coin': amount,
                                'factual_amount_usd': 0 if status in [OrderStatus.PROCESSING,
                                                                      OrderStatus.NOT_EXECUTED] \
                                    else amount * price,
                                'datetime_update': datetime.utcnow(),
                                'ts_update': time.time() * 1000
                            }
                            break

            return result

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
        res =  requests.get(headers=headers, url=self.BASE_URL + url_path).json()
        self.balance['total'] = res['accounts']['flex']['balanceValue']

    def fit_amount(self, amount) -> None:
        if not self.quantity_precision:
            if '.' in str(self.step_size):
                round_amount_len = len(str(self.step_size).split('.')[1])
            else:
                round_amount_len = 0
            amount = str(round(amount - (amount % self.step_size), round_amount_len))
        else:
            amount = str(round(float(round(amount / self.step_size) * self.step_size), self.quantity_precision))

        self.expect_amount_coin = float(amount)

    async def __create_order(self, price: float, side: str, session: aiohttp.ClientSession,
                             expire=5000, client_id=None) -> dict:
        nonce = str(int(time.time() * 1000))
        url_path = "/derivatives/api/v3/sendorder"
        self.expect_price = float(round(float(round(price / self.tick_size) * self.tick_size), self.price_precision))
        params = {
            "orderType": "lmt",
            "limitPrice": self.expect_price,
            "side": side.lower(),
            "size": self.expect_amount_coin,
            "symbol": self.symbol
        }
        post_string = "&".join([f"{key}={params[key]}" for key in sorted(params)])

        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "Nonce": nonce,
            "APIKey": self.__api_key,
            "Authent": self.get_kraken_futures_signature(url_path, post_string, nonce).decode('utf-8'),
        }
        async with session.post(
                url=self.BASE_URL + url_path + '?' + post_string, headers=headers, data=post_string
        ) as resp:
            response = await resp.json()
            print(f'KRAKEN RESPONSE: {response}')
            self.LAST_ORDER_ID = response.get('sendStatus', {}).get('order_id', 'default')
            try:
                timestamp = response['sendStatus']['orderEvents'][0]['orderPriorExecution']['timestamp']
                timestamp = int(datetime.timestamp(datetime.strptime(timestamp, '%Y-%m-%dT%H:%M:%S.%fZ')) * 1000)
                if response.get('sendStatus', {}).get('status'):
                    status = ResponseStatus.SUCCESS

            except:
                timestamp = 0000000000000
                status = ResponseStatus.ERROR
                self.error_info = response

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
                    self.positions.update({payload['symbol'].lower(): {
                        'side': PositionSideEnum.LONG if payload['side'] == 'long' else PositionSideEnum.SHORT,
                        'amount_usd': payload['size'] * payload['price'],
                        'amount': payload['size'],
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
                                "feed": "open_positions",
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
                                self.balance['total'] = msg_data['flex_futures']['balance_value']

                        elif msg_data.get('feed') == 'open_positions':
                            for position in msg_data.get('positions', []):
                                self.positions.update({position['instrument'].lower(): {
                                    'side': 'LONG' if position['balance'] >= 0 else 'SHORT',
                                    'amount_usd': position['balance'] * position['mark_price'],
                                    'amount': position['balance'],
                                    'entry_price': position['entry_price'],
                                    'unrealized_pnl_usd': 0,
                                    'realized_pnl_usd': 0,
                                    'lever': self.leverage
                                }})
                        elif msg_data.get('feed') == 'open_orders' and msg_data.get('order'):
                            order = msg_data['order']
                            self.last_price['sell' if order['direction'] else 'buy'] = float(order['limit_price'])
                            status = OrderStatus.PROCESSING

                            if msg_data['reason'] == 'full_fill':
                                status = OrderStatus.FULLY_EXECUTED
                            elif msg_data['reason'] == 'partial_fill':
                                status = OrderStatus.PARTIALLY_EXECUTED
                            elif msg_data['reason'] in ['cancelled_by_user', 'cancelled_by_admin', 'not_enough_margin']:
                                status = OrderStatus.NOT_EXECUTED

                            if status:
                                price = 0 if status in [OrderStatus.PROCESSING, OrderStatus.NOT_EXECUTED] \
                                    else order['limit_price']
                                amount = 0 if status in [OrderStatus.PROCESSING, OrderStatus.NOT_EXECUTED] \
                                    else order['filled']
                                result = {
                                    'exchange_order_id': order['order_id'],
                                    'exchange': self.EXCHANGE_NAME,
                                    'status': status,
                                    'factual_price': price,
                                    'factual_amount_coin': amount,
                                    'factual_amount_usd': 0 if status in [OrderStatus.PROCESSING,
                                                                          OrderStatus.NOT_EXECUTED] \
                                        else amount * price,
                                    'datetime_update': datetime.utcnow(),
                                    'ts_update': int(time.time() * 1000)
                                }

                                if self.symbol.upper() == order['instrument'].upper() \
                                        and result['status'] != OrderStatus.PROCESSING:
                                    self.orders.update({order['order_id']: result})
