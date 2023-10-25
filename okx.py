import traceback

import aiohttp
import asyncio
import time
import hmac
import base64
import json
import threading
import string
from datetime import datetime
import requests
import random
import queue
import telebot
import uuid

from core.base_client import BaseClient
from clients.enums import ResponseStatus, OrderStatus, ClientsOrderStatuses


class OkxClient(BaseClient):
    URI_WS_AWS = "wss://wsaws.okx.com:8443/ws/v5/public"
    URI_WS_PRIVATE = "wss://wsaws.okx.com:8443/ws/v5/private"
    headers = {'Content-Type': 'application/json'}
    EXCHANGE_NAME = 'OKX'

    def __init__(self, keys, leverage, alert_id, alert_token, markets_list=[], max_pos_part=20):
        self.max_pos_part = max_pos_part
        self.markets_list = markets_list
        self.chat_id = int(alert_id)
        self.telegram_bot = telebot.TeleBot(alert_token)
        self.create_order_response = False
        self.symbol = keys['symbol']
        self.leverage = leverage
        self.public_key = keys['API_KEY']
        self.secret_key = keys['API_SECRET']
        self.passphrase = keys['PASSPHRASE']
        self.positions = {}
        self._loop_public = asyncio.new_event_loop()
        self._loop_private = asyncio.new_event_loop()
        self.queue = queue.Queue()
        self._connected = asyncio.Event()
        self.wst_public = threading.Thread(target=self._run_ws_forever, args=['public', self._loop_public])
        self.wst_private = threading.Thread(target=self._run_ws_forever, args=['private', self._loop_private])
        self.instruments = self.get_instruments()
        self.markets = self.get_markets()

        self.price = 0
        self.amount = 0
        self.orderbook = {}
        self.orders = {}
        self.last_price = {}
        self.balance = {'free': 0, 'total': 0}
        self.taker_fee = 0
        self.start_time = int(datetime.utcnow().timestamp())
        self.time_sent = datetime.utcnow().timestamp()

    @staticmethod
    def id_generator(size=12, chars=string.digits):
        return ''.join(random.choice(chars) for _ in range(size))

    def run_updater(self):
        self.wst_public.daemon = True
        self.wst_public.start()
        self.wst_private.daemon = True
        self.wst_private.start()

    async def _login(self, ws, event):
        request_path = '/users/self/verify'
        timestamp = str(int(round(time.time())))
        signature = self.signature(timestamp, 'GET', request_path, None)
        msg = {"op": "login",
               "args": [{
                   "apiKey": self.public_key,
                   "passphrase": self.passphrase,
                   "timestamp": timestamp,
                   "sign": signature
               }]}
        await event.wait()
        await ws.send_json(msg)

    def signature(self, timestamp, method, request_path, body):
        if str(body) == '{}' or str(body) == 'None':
            body = ''
        message = str(timestamp) + str.upper(method) + request_path + str(body)

        mac = hmac.new(bytes(self.secret_key, encoding='utf8'), bytes(message, encoding='utf-8'), digestmod='sha256')
        signature = mac.digest()

        return base64.b64encode(signature).decode('UTF-8')

    async def _subscribe_orderbooks(self):
        # for symbol in list(self.markets.values())[:10]:
        for symbol in self.markets_list:
            if market := self.markets.get(symbol):
                msg = {
                    "op": "subscribe",
                    "args": [{
                        "channel": "books5",  # 0-l2-tbt",
                        "instId": market
                    }]}
                await self._connected.wait()
                await self._ws_public.send_json(msg)

    async def _subscribe_account(self):
        msg = {
            "op": "subscribe",
            "args": [{
                "channel": "account"
            }]}
        await self._connected.wait()
        try:
            await self._ws_private.send_json(msg)
        except Exception as e:
            traceback.print_exc()

    async def _subscribe_positions(self):
        for symbol in self.markets_list:
            if market := self.markets.get(symbol):
                msg = {
                    "op": "subscribe",
                    "args": [
                        {
                            "channel": "positions",
                            "instType": "SWAP",
                            "instFamily": market.split('-SWAP')[0],
                            "instId": market
                        }
                    ]
                }
                await self._connected.wait()
                await self._ws_private.send_json(msg)

    async def _subscribe_orders(self):
        for symbol in self.markets_list:
            if market := self.markets.get(symbol):
                msg = {
                    "op": "subscribe",
                    "args": [
                        {
                            "channel": "orders",
                            "instType": "SWAP",
                            "instFamily": market.split('-SWAP')[0],
                            "instId": market
                        }
                    ]
                }
                await self._connected.wait()
                await self._ws_private.send_json(msg)

    def _run_ws_forever(self, type, loop):
        while True:
            try:
                loop.run_until_complete(self._run_ws_loop(type))
            except Exception as e:
                traceback.print_exc()
                print(f"Line 100. Error: {e}")
            finally:
                print(f"WS loop {type} completed. Restarting")

    async def _run_ws_loop(self, type):
        async with aiohttp.ClientSession() as s:
            if type == 'private':
                endpoint = self.URI_WS_PRIVATE
            else:
                endpoint = self.URI_WS_AWS
            async with s.ws_connect(endpoint) as ws:
                print(f"OKEX: connected {type}")
                self._connected.set()
                # try:
                if type == 'private':
                    self._ws_private = ws
                    self._loop_private.create_task(self._login(ws, self._connected))
                    async for msg in ws:
                        if json.loads(msg.data).get('event'):
                            break
                    self._loop_private.create_task(self._subscribe_account())
                    self._loop_private.create_task(self._subscribe_positions())
                    self._loop_private.create_task(self._subscribe_orders())
                else:
                    self._ws_public = ws
                    self._loop_public.create_task(self._login(ws, self._connected))
                    self._loop_public.create_task(self._subscribe_orderbooks())
                async for msg in ws:
                    try:
                        args = self.queue.get_nowait()
                        print(f"ORDER SENDING: {args}")
                        await self._send_order(**args)
                    except queue.Empty:
                        await self._ping(ws)
                    self._process_msg(msg)

    async def _ping(self, ws):
        time_from = int(int(round(datetime.utcnow().timestamp())) - self.start_time) % 5
        if not time_from:
            await ws.ping(b'PING')
            self.start_time -= 1

    def get_position(self):
        way = 'https://www.okx.com/api/v5/account/positions'
        headers = self.get_private_headers('GET', '/api/v5/account/positions')
        resp = requests.get(url=way, headers=headers).json()
        for pos in resp['data']:
            side = 'LONG' if float(pos['pos']) > 0 else 'SHORT'
            amount_usd = float(pos['notionalUsd'])
            if side == 'SHORT':
                amount = -amount_usd / float(pos['markPx'])
            else:
                amount = amount_usd / float(pos['markPx'])
            self.positions.update({pos['instId']: {'side': side,
                                                   'amount_usd': amount_usd,
                                                   'amount': amount,
                                                   'entry_price': float(pos['avgPx']),
                                                   'unrealized_pnl_usd': float(pos['upl']),
                                                   'realized_pnl_usd': 0,
                                                   'lever': self.leverage}})

    def _update_positions(self, obj):
        if not obj['data']:
            return
        if obj['data'][0]['pos'] != '0':
            side = 'LONG' if float(obj['data'][0]['pos']) > 0 else 'SHORT'
            amount_usd = float(obj['data'][0]['notionalUsd'])
            if side == 'SHORT':
                amount = -amount_usd / float(obj['data'][0]['markPx'])
            else:
                amount = amount_usd / float(obj['data'][0]['markPx'])
            self.positions.update({obj['arg']['instId']: {'side': side,
                                                          'amount_usd': amount_usd,
                                                          'amount': amount,
                                                          'entry_price': float(obj['data'][0]['avgPx']),
                                                          'unrealized_pnl_usd': float(obj['data'][0]['upl']),
                                                          'realized_pnl_usd': 0,
                                                          'lever': self.leverage}})
        else:
            self.positions.update({obj['arg']['instId']: {'side': 'LONG',
                                                          'amount_usd': 0,
                                                          'amount': 0,
                                                          'entry_price': 0,
                                                          'unrealized_pnl_usd': 0,
                                                          'realized_pnl_usd': 0,
                                                          'lever': self.leverage}})
        # print(self.positions)
        # for one in obj['data'][0]:
        #     if obj['data'][0][one]:
        #         self.positions[obj['arg']['instId']].update({one: obj['data'][0][one]})

    def _update_orderbook(self, obj):
        symbol = obj['arg']['instId']
        orderbook = obj['data'][0]
        self.orderbook.update({symbol: {'asks': [[float(x[0]), float(x[1]), int(x[3])] for x in orderbook['asks']],
                                        'bids': [[float(x[0]), float(x[1]), int(x[3])] for x in orderbook['bids']],
                                        'timestamp': int(orderbook['ts'])}})

    def _update_account(self, obj):
        resp = obj['data']
        if len(resp):
            acc_data = resp[0]['details'][0]
            self.balance = {'free': float(acc_data['availBal']),
                            'total': float(acc_data['availBal']) + float(acc_data['frozenBal']),
                            'timestamp': int(resp[0]['uTime'])}
        else:
            self.balance = {'free': 0,
                            'total': 0,
                            'timestamp': int(round(datetime.utcnow().timestamp() * 1000))}

    def _update_orders(self, obj):
        if obj.get('data') and obj.get('arg'):
            for order in obj.get('data'):
                print(f"OKEX RESPONSE: {order}\n")
                status = None
                if order['state'] == 'live':
                    if float(order['px']) == self.price and float(order['sz']) == self.amount:
                        self.create_order_response = True
                    print(f"OKEX ORDER PLACE TIME: {float(order['uTime']) - self.time_sent} ms\n")
                    if self.orders.get(order['ordId']):
                        continue
                    else:
                        status = OrderStatus.PROCESSING
                if order['state'] == 'filled':
                    status = OrderStatus.FULLY_EXECUTED
                elif order['state'] == 'canceled' and order['fillSz'] != '0' and order['fillSz'] != order['sz']:
                    status = OrderStatus.PARTIALLY_EXECUTED
                elif order['state'] == 'partially_filled':
                    status = OrderStatus.PARTIALLY_EXECUTED
                elif order['state'] == 'canceled' and order['fillSz'] == '0':
                    status = OrderStatus.NOT_EXECUTED
                self.get_taker_fee(order)
                contract_value = self.get_contract_value(order['instId'])
                result = {
                    'exchange_order_id': order['ordId'],
                    'exchange': self.EXCHANGE_NAME,
                    'status': status,
                    'factual_price': float(order['fillPx']) if order['fillPx'] else 0,
                    'factual_amount_coin': float(order['fillSz']) * contract_value if order['fillSz'] else 0,
                    'factual_amount_usd': float(order['fillNotionalUsd']) if order['fillNotionalUsd'] else 0,
                    'datetime_update': datetime.utcnow(),
                    'ts_update': int(round(datetime.utcnow().timestamp() * 1000))
                }
                self.orders.update({order['ordId']: result})

    def get_taker_fee(self, order):
        if not self.taker_fee:
            if order['fillNotionalUsd']:
                self.taker_fee = abs(float(order['fillFee'])) / float(order['fillNotionalUsd'])
                print(self.taker_fee, 'TAKER FEE')

    def _process_msg(self, msg: aiohttp.WSMessage):
        obj = json.loads(msg.data)
        if obj.get('event'):
            return
        if obj.get('arg'):
            if obj['arg']['channel'] == 'account':
                self._update_account(obj)
            elif obj['arg']['channel'] in ['bbo-tbt', 'books50-l2-tbt', 'books5']:
                self._update_orderbook(obj)
            elif obj['arg']['channel'] == 'positions':
                self._update_positions(obj)
            elif obj['arg']['channel'] == 'orders':
                self._update_orders(obj)

    def get_contract_value(self, symbol):
        for instrument in self.instruments:
            if instrument['instId'] == symbol:
                contract_value = float(instrument['ctVal'])
                return contract_value

    def get_sizes_for_symbol(self, symbol):
        for instrument in self.instruments:
            if instrument['instId'] == symbol:
                tick_size = float(instrument['tickSz'])
                step_size = float(instrument['lotSz'])
                contract_value = float(instrument['ctVal'])
                min_size = float(instrument['minSz'])
                quantity_precision = len(str(step_size).split('.')[1]) if '.' in str(step_size) else 1
                return tick_size, step_size, quantity_precision, contract_value, min_size

    def fit_sizes(self, amount, price, symbol) -> None:
        tick_size, step_size, quantity_precision, contract_value, min_size = self.get_sizes_for_symbol(symbol)
        amount = amount / contract_value
        if min_size > amount:
            print(f"\n\nOKEX {symbol} ORDER LESS THAN MIN SIZE: {min_size}\n\n")
        rounded_amount = round(amount / step_size) * step_size
        self.amount = round(rounded_amount, quantity_precision)
        if '.' in str(tick_size):
            round_price_len = len(str(tick_size).split('.')[1])
        elif '-' in str(tick_size):
            round_price_len = int(str(tick_size).split('-')[1])
        else:
            round_price_len = 0
        rounded_price = round(price / tick_size) * tick_size
        self.price = round(rounded_price, round_price_len)

    async def create_order(self, symbol, side, session: aiohttp.ClientSession, expire=100, client_id=None) -> dict:
        self.time_sent = int(round((datetime.utcnow().timestamp()) * 1000))
        self.queue.put_nowait({'symbol': symbol,
                               'amount': self.amount,
                               'price': self.price,
                               'side': side,
                               'expire': expire})
        while not self.create_order_response or datetime.utcnow().timestamp() - (self.time_sent / 1000) < 5:
            time.sleep(0.01)
        response = {
            'exchange_name': self.EXCHANGE_NAME,
            'timestamp': int(round((datetime.utcnow().timestamp()) * 1000)),
            'status': ResponseStatus.SUCCESS if self.create_order_response else ResponseStatus.ERROR
        }
        self.create_order_response = False
        return response

    async def _send_order(self, symbol, amount, price, side, expire=100):
        # expire_date = str(round((datetime.utcnow().timestamp() + expire) * 1000))
        msg = {
            "id": self.id_generator(),
            "op": "order",
            "args": [
                {
                    "side": side,
                    "instId": symbol,
                    "tdMode": "cross",
                    "ordType": 'limit',
                    "sz": amount,
                    # "expTime": expire_date,
                    "px": price
                }
            ]
        }
        await self._ws_private.send_json(msg)

    @staticmethod
    def get_timestamp():
        now = datetime.utcnow()
        t = now.isoformat("T", "milliseconds")
        return t + "Z"

    def get_instruments(self):
        way = f'https://www.okx.com/api/v5/public/instruments?instType=SWAP'
        resp = requests.get(url=way, headers=self.headers).json()
        return resp['data']

    def get_markets(self):
        markets = {}
        for instrument in self.instruments:
            coin = instrument['ctValCcy']
            market = instrument['instId']
            if instrument['state'] == 'live':
                if instrument['settleCcy'] == 'USDT':
                    markets.update({coin: market})
            else:
                message = f"{self.EXCHANGE_NAME}:\n{market} has status {instrument['state']}"
                try:
                    self.telegram_bot.send_message(self.chat_id, '<pre>' + message + '</pre>', parse_mode='HTML')
                except:
                    pass
            # print(inst['instId'], inst, '\n')
        return markets

    def get_available_balance(self):
        available_balances = {}
        position_value = 0
        position_value_abs = 0
        available_margin = self.balance['total'] * self.leverage
        avl_margin_per_market = available_margin / 100 * self.max_pos_part
        for symbol, position in self.positions.items():
            if position.get('amount_usd'):
                position_value += position['amount_usd']
                position_value_abs += abs(position['amount_usd'])
                if position['amount_usd'] < 0:
                    available_balances.update({symbol: {'buy': avl_margin_per_market + position['amount_usd'],
                                                        'sell': avl_margin_per_market - position['amount_usd']}})
                else:
                    available_balances.update({symbol: {'buy': avl_margin_per_market - position['amount_usd'],
                                                        'sell': avl_margin_per_market + position['amount_usd']}})
        if position_value_abs < available_margin:
            available_balances['buy'] = available_margin - position_value
            available_balances['sell'] = available_margin + position_value
        else:
            for symbol, position in self.positions.items():
                if position.get('amount_usd'):
                    if position['amount_usd'] < 0:
                        available_balances.update({symbol: {'buy': abs(position['amount_usd']),
                                                            'sell': 0}})
                    else:
                        available_balances.update({symbol: {'buy': 0,
                                                            'sell': position['amount_usd']}})
            available_balances['buy'] = 0
            available_balances['sell'] = 0
        return available_balances

    def get_positions(self):
        return self.positions

    def get_private_headers(self, method, way, body=None):
        dt = datetime.utcnow()
        formatted_datetime = dt.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        timestamp = str(formatted_datetime)[:-4] + 'Z'
        signature = self.signature(timestamp, method, way, body)
        headers = {'OK-ACCESS-KEY': self.public_key,
                   'OK-ACCESS-SIGN': signature,
                   'OK-ACCESS-TIMESTAMP': timestamp,
                   'OK-ACCESS-PASSPHRASE': self.passphrase}
        return headers

    def get_real_balance(self):
        way = 'https://www.okx.com/api/v5/account/balance?ccy=USDT'
        headers = self.get_private_headers('GET', '/api/v5/account/balance?ccy=USDT')
        headers.update(self.headers)
        resp = requests.get(url=way, headers=headers).json()
        if resp.get('code') == '0':
            self.balance = {'free': float(resp['data'][0]['details'][0]['availEq']),
                            'total': float(resp['data'][0]['details'][0]['eq']),
                            'timestamp': datetime.utcnow().timestamp()}
        # {'code': '0', 'data': [{'adjEq': '', 'borrowFroz': '', 'details': [
        #     {'availBal': '466.6748538968118', 'availEq': '466.6748538968118', 'borrowFroz': '',
        #      'cashBal': '500.0581872301451', 'ccy': 'USDT', 'crossLiab': '', 'disEq': '500.25821050503714',
        #      'eq': '500.0581872301451', 'eqUsd': '500.25821050503714', 'fixedBal': '0',
        #      'frozenBal': '33.38333333333333', 'interest': '', 'isoEq': '0', 'isoLiab': '', 'isoUpl': '0', 'liab': '',
        #      'maxLoan': '', 'mgnRatio': '', 'notionalLever': '0', 'ordFrozen': '33.333333333333336', 'spotInUseAmt': '',
        #      'spotIsoBal': '0', 'stgyEq': '0', 'twap': '0', 'uTime': '1698244053657', 'upl': '0', 'uplLiab': ''}],
        #                         'imr': '', 'isoEq': '0', 'mgnRatio': '', 'mmr': '', 'notionalUsd': '', 'ordFroz': '',
        #                         'totalEq': '500.25821050503714', 'uTime': '1698245152624'}], 'msg': ''}

    def get_orderbook(self, symbol):
        while not self.orderbook.get(symbol):
            print(f"{self.EXCHANGE_NAME}: CAN'T GET OB {symbol}")
            time.sleep(0.01)
        return self.orderbook[symbol]

    def get_last_price(self, side):
        return self.last_price[side]

    def get_orders(self):
        return self.orders

    def cancel_all_orders(self, orderID=None):
        pass

    def get_orderbook_by_symbol(self, symbol):
        pass

if __name__ == '__main__':
    import configparser
    import sys

    config = configparser.ConfigParser()
    config.read(sys.argv[1], "utf-8")
    client = OkxClient(config['OKX'],
                       float(config['SETTINGS']['LEVERAGE']),
                       int(config['TELEGRAM']['ALERT_CHAT_ID']),
                       config['TELEGRAM']['ALERT_BOT_TOKEN'],
                       max_pos_part=int(config['SETTINGS']['PERCENT_PER_MARKET']),
                       markets_list=['ETH', 'BTC', 'LTC', 'BCH', 'SOL', 'MINA', 'XRP', 'PEPE', 'CFX', 'FIL'])
    # client.run_updater()

    time.sleep(5)
    # print(client.get_available_balance())
    # price = client.get_orderbook('SOL-USDT-SWAP')['asks'][4][0]
    # client.fit_sizes(1, price, 'SOL-USDT-SWAP')
    client.get_position()
    print(client.positions)
    client.get_real_balance()
    print(client.balance)
    # print(client.get_available_balance())



    async def test_order():
        async with aiohttp.ClientSession() as session:
            data = await client.create_order('SOL-USDT-SWAP',
                                             'buy',
                                             session=session,
                                             client_id=f"api_deal_{str(uuid.uuid4()).replace('-', '')[:20]}")
            print(data)


    # asyncio.run(test_order())
    # time.sleep(5)
    # client.get_position()
    while True:
        time.sleep(5)
    # client.get_markets()

# import asyncio
# import aiohttp
#
# class WebSocketClient:
#     def __init__(self, url):
#         self.url = url
#         self.ws = None
#         self.lock = asyncio.Lock()
#         self.connected = asyncio.Event()
#
#     async def connect(self):
#         async with self.lock:
#             self.ws = await aiohttp.ClientSession().ws_connect(self.url)
#             self.connected.set()
#
#     async def disconnect(self):
#         async with self.lock:
#             if self.ws:
#                 await self.ws.close()
#                 self.connected.clear()
#
#     async def send_message(self, message):
#         async with self.lock:
#             if not self.connected.is_set():
#                 await self.connect()
#             await self.ws.send_str(message)
#
#     async def receive_messages(self):
#         while True:
#             try:
#                 async with self.lock:
#                     if not self.connected.is_set():
#                         await self.connect()
#                     msg = await self.ws.receive()
#                     if msg.type == aiohttp.WSMsgType.TEXT:
#                         print(f"Received: {msg.data}")
#                     elif msg.type == aiohttp.WSMsgType.CLOSED:
#                         print("WebSocket closed by the server.")
#                         await self.disconnect()
#                         break
#                     elif msg.type == aiohttp.WSMsgType.ERROR:
#                         print("WebSocket error occurred.")
#                         await self.disconnect()
#                         break
#             except Exception as e:
#                 print(f"WebSocket error: {e}")
#                 await self.disconnect()
#
# async def main():
#     url = "wss://example.com/ws"  # Replace with your WebSocket URL
#     client = WebSocketClient(url)
#
#     while True:
#         try:
#             await client.receive_messages()
#         except KeyboardInterrupt:
#             break
#         except Exception as e:
#             print(f"Error in main loop: {e}")
#
# if __name__ == "__main__":
#     asyncio.run(main())
