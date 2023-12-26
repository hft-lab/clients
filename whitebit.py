import time
import aiohttp
import json
import requests
from datetime import datetime
import asyncio
import threading
import hmac
import hashlib
import base64

from clients.core.enums import ResponseStatus, OrderStatus, ClientsOrderStatuses
from core.wrappers import try_exc_regular, try_exc_async
from clients.core.base_client import BaseClient


class WhiteBitClient(BaseClient):
    PUBLIC_WS_ENDPOINT = 'wss://api.whitebit.com/ws'
    BASE_URL = 'https://whitebit.com'
    EXCHANGE_NAME = 'WHITEBIT'

    def __init__(self, keys=None, leverage=None, markets_list=[], max_pos_part=20):
        super().__init__()
        if keys:
            self.api_key = keys['API_KEY']
            self.api_secret = keys['API_SECRET']
        self.headers = {"Accept": "application/json;charset=UTF-8",
                        "Content-Type": "application/json",
                        'User-Agent': 'python-whitebit-sdk'}
        self.instruments = {}
        self.leverage = leverage
        self.max_pos_part = max_pos_part
        self.price = 0
        self.amount = 0
        self.markets = self.get_markets()
        self._loop = asyncio.new_event_loop()
        self._connected = asyncio.Event()
        self.wst_public = threading.Thread(target=self._run_ws_forever, args=[self._loop])
        self.requestLimit = 1200
        self.getting_ob = asyncio.Event()
        self.now_getting = ''
        self.orderbook = {}
        self.orders = {}
        self.balance = {}
        self.positions = {}
        self.websocket_token = self.get_ws_token()
        self.get_real_balance()
        self.get_position()
        self.taker_fee = 0.00035

    @try_exc_regular
    def get_markets(self):
        path = "/api/v4/public/markets"
        resp = requests.get(url=self.BASE_URL + path, headers=self.headers).json()
        markets = {}
        for market in resp:
            if market['type'] == 'futures' and market['tradesEnabled']:
                markets.update({market['stock']: market['name']})
                price_precision = int(market['moneyPrec'])
                step_size = float(market['minAmount'])
                tick_size = self.calculate_tick_size(price_precision)
                quantity_precision = int(market['stockPrec'])
                self.instruments[market['name']] = {'tick_size': tick_size,
                                                    'step_size': step_size,
                                                    'quantity_precision': quantity_precision,
                                                    'price_precision': price_precision,
                                                    'min_size': step_size}
        return markets

    @try_exc_regular
    def set_leverage(self, leverage):
        path = "/api/v4/collateral-account/leverage"
        params = {'leverage': leverage}
        params, headers = self.get_auth_for_request(params, path)
        path += self._create_uri(params)
        res = requests.post(url=self.BASE_URL + path, headers=headers, json=params)
        return res.json()

    @try_exc_regular
    def cancel_all_orders(self):
        path = '/api/v4/order/cancel/all'
        params, headers = self.get_auth_for_request({}, path)
        path += self._create_uri(params)
        res = requests.post(url=self.BASE_URL + path, headers=headers, json=params)
        return res.json()

    @try_exc_regular
    def get_position(self):
        path = "/api/v4/collateral-account/positions/open"
        params, headers = self.get_auth_for_request({}, path)
        path += self._create_uri(params)
        res = requests.post(url=self.BASE_URL + path, headers=headers, json=params)
        response = res.json()
        print(response)

    @try_exc_regular
    def get_real_balance(self):
        path = "/api/v4/trade-account/balance"
        params, headers = self.get_auth_for_request({}, path)
        path += self._create_uri(params)
        res = requests.post(url=self.BASE_URL + path, headers=headers, json=params)
        response = res.json()
        self.balance = {'timestamp': datetime.utcnow().timestamp(),
                        'total': float(response['USDT']['available']) + float(response['USDT']['freeze']),
                        'free': float(response['USDT']['available'])}

    @staticmethod
    @try_exc_regular
    def calculate_precision(precision):
        return max(len(str(precision).split('.')[-1]), 0) if '.' in str(precision) else 0

    @staticmethod
    @try_exc_regular
    def calculate_tick_size(precision):
        return float(f"1e-{precision}")

    @try_exc_async
    async def get_orderbook_by_symbol(self, symbol):
        async with aiohttp.ClientSession() as session:
            path = f'/api/v4/public/orderbook/{symbol}'
            params = {'limit': 10}  # Adjusting parameters as per WhiteBit's documentation
            path += self._create_uri(params)
            async with session.get(url=self.BASE_URL + path, headers=self.headers) as resp:
                ob = await resp.json()
                # Check if the response is a dictionary and has 'asks' and 'bids' directly within it
                if isinstance(ob, dict) and 'asks' in ob and 'bids' in ob:
                    orderbook = {
                        'asks': [[float(ask[0]), float(ask[1])] for ask in ob['asks']],
                        'bids': [[float(bid[0]), float(bid[1])] for bid in ob['bids']]
                    }
                    return orderbook

    @try_exc_regular
    def get_signature(self, data: dict):
        data_json = json.dumps(data, separators=(',', ':'))  # use separators param for deleting spaces
        payload = base64.b64encode(data_json.encode('ascii'))
        signature = hmac.new(self.api_secret.encode('ascii'), payload, hashlib.sha512).hexdigest()
        return signature, payload

    @staticmethod
    @try_exc_regular
    def _create_uri(params) -> str:
        data = ''
        strl = []
        for key in sorted(params):
            strl.append(f'{key}={params[key]}')
        data += '&'.join(strl)
        return f'?{data}'.replace(' ', '%20')

    @try_exc_regular
    def get_auth_for_request(self, params, uri):
        params['request'] = uri
        params['nonce'] = int(time.time() * 1000)
        params['nonceWindow'] = True
        signature, payload = self.get_signature(params)
        headers = self.headers
        headers.update({
            'X-TXC-APIKEY': self.api_key,
            'X-TXC-SIGNATURE': signature,
            'X-TXC-PAYLOAD': payload.decode('ascii'),
        })
        return params, headers

    @try_exc_regular
    def _run_ws_forever(self, loop):
        while True:
            loop.run_until_complete(self._run_ws_loop())

    @try_exc_regular
    def get_orders(self):
        # NECESSARY
        return self.orders

    @try_exc_regular
    def get_balance(self):
        return self.balance['total']

    @try_exc_async
    async def _run_ws_loop(self):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(self.PUBLIC_WS_ENDPOINT) as ws:
                self._connected.set()
                self._ws = ws
                await self._loop.create_task(self.subscribe_privates())
                for market in self.markets.values():
                    await self._loop.create_task(self.subscribe_orderbooks(market))
                async for msg in ws:
                    data = json.loads(msg.data)
                    if data.get('method') == 'depth_update':
                        if data['params'][0]:
                            self.update_orderbook_snapshot(data)
                        else:
                            self.update_orderbook(data)
                    elif data.get('method') == 'balanceMargin_update':
                        self.update_balances(data)
                    elif data.get('method') in ['ordersExecuted_update', 'ordersPending_update']:
                        self.update_orders(data)

    @try_exc_regular
    def update_balances(self, data):
        print('BALANCES', data)
        pass

    @try_exc_regular
    def update_orders(self, data):
        print('ORDERS', data)
        pass

    @try_exc_regular
    def fit_sizes(self, price, symbol):
        # NECESSARY
        instr = self.instruments[symbol]
        tick_size = instr['tick_size']
        quantity_precision = instr['quantity_precision']
        price_precision = instr['price_precision']
        self.amount = round(self.amount, quantity_precision)
        rounded_price = round(price / tick_size) * tick_size
        self.price = round(rounded_price, price_precision)

    @try_exc_regular
    def get_available_balance(self):
        return super().get_available_balance(
            leverage=self.leverage,
            max_pos_part=self.max_pos_part,
            positions=self.positions,
            balance=self.balance)

    @try_exc_regular
    def get_last_price(self):
        pass

    @try_exc_regular
    def get_positions(self):
        return self.positions

    @try_exc_regular
    def get_order_by_id(self, order_id):
        path = '/api/v4/trade-account/order'
        params = {"orderId": order_id}
        params, headers = self.get_auth_for_request(params, path)
        path += self._create_uri(params)
        res = requests.post(url=self.BASE_URL + path, headers=headers, json=params)
        response = res.json()
        print(response)

    @try_exc_async
    async def create_order(self, symbol, side, session=None, expire=10000, client_id=None):
        path = "/api/v4/order/new"
        params = {
            "market": symbol,
            "side": side,
            "amount": self.amount,
            "price": self.price,
            "postOnly": False,
            "ioc": False
        }
        print(f"{self.EXCHANGE_NAME} SENDING ORDER: {params}")
        if client_id:
            params['clientOrderId'] = client_id
        params, headers = self.get_auth_for_request(params, path)
        path += self._create_uri(params)
        async with session.post(url=self.BASE_URL + path, headers=headers, json=params) as resp:
            response = await resp.json()
            print(f"{self.EXCHANGE_NAME} ORDER CREATE RESPONSE: {response}")

    @try_exc_async
    async def subscribe_orderbooks(self, symbol):
        data = [symbol, 10, '0', True]
        method = {"id": 8888,
                  "method": "depth_subscribe",
                  "params": data}
        await self._connected.wait()
        await self._ws.send_json(method)

    @try_exc_regular
    def get_ws_token(self):
        path = "/api/v4/profile/websocket_token"
        params, headers = self.get_auth_for_request({}, path)
        path += self._create_uri(params)
        res = requests.post(url=self.BASE_URL + path, headers=headers, json=params).json()
        return res['websocket_token']

    @try_exc_async
    async def subscribe_privates(self):
        method_auth = {"id": 8888, "method": "authorize", "params": [self.websocket_token, "public"]}
        orders_ex = {"id": 8888, "method": "ordersExecuted_subscribe", "params": [list(self.markets.values()), 0]}
        orders_pend = {"id": 8888, "method": "ordersPending_subscribe", "params": [list(self.markets.values()), 0]}
        balance = {"id": 8888, "method": "balanceMargin_subscribe", "params": ["USDT"]}
        await self._connected.wait()
        await self._ws.send_json(method_auth)
        time.sleep(1)
        await self._ws.send_json(orders_ex)
        await self._ws.send_json(orders_pend)
        await self._ws.send_json(balance)
        await self._ws.send_json(balance)

    @try_exc_regular
    def get_all_tops(self):
        tops = {}
        for coin, symbol in self.markets.items():
            orderbook = self.get_orderbook(symbol)
            if orderbook and orderbook['bids'] and orderbook['asks']:
                tops.update({self.EXCHANGE_NAME + '__' + coin: {
                    'top_bid': orderbook['bids'][0][0], 'top_ask': orderbook['asks'][0][0],
                    'bid_vol': orderbook['bids'][0][1], 'ask_vol': orderbook['asks'][0][1],
                    'ts_exchange': orderbook['timestamp'] * 1000}})
        return tops

    @try_exc_regular
    def update_orderbook(self, data):
        if data['params'][2] == self.now_getting:
            while self.getting_ob.is_set():
                time.sleep(0.00001)
        symbol = data['params'][2]
        for new_bid in data['params'][1].get('bids', []):
            res = self.orderbook[symbol]['bids']
            if res.get(new_bid[0]) and new_bid[1] == '0':
                del res[new_bid[0]]
            else:
                self.orderbook[symbol]['bids'][new_bid[0]] = new_bid[1]
        for new_ask in data['params'][1].get('asks', []):
            res = self.orderbook[symbol]['asks']
            if res.get(new_ask[0]) and new_ask[1] == '0':
                del res[new_ask[0]]
            else:
                self.orderbook[symbol]['asks'][new_ask[0]] = new_ask[1]
        self.orderbook[symbol]['timestamp'] = data['params'][1]['timestamp']

    @try_exc_regular
    def update_orderbook_snapshot(self, data):
        if data['params'][2] == self.now_getting:
            while self.getting_ob.is_set():
                time.sleep(0.00001)
        symbol = data['params'][2]
        self.orderbook[symbol] = {'asks': {x[0]: x[1] for x in data['params'][1].get('asks', [])},
                                  'bids': {x[0]: x[1] for x in data['params'][1].get('bids', [])},
                                  'timestamp': data['params'][1]['timestamp']}

    @try_exc_regular
    def get_orderbook(self, symbol) -> dict:
        if not self.orderbook.get(symbol):
            return {}
        self.getting_ob.set()
        self.now_getting = symbol
        snap = self.orderbook[symbol]
        ob = {'timestamp': self.orderbook[symbol]['timestamp'],
              'asks': [[float(x), float(snap['asks'][x])] for x in sorted(snap['asks']) if snap['asks'].get(x)],
              'bids': [[float(x), float(snap['bids'][x])] for x in sorted(snap['bids']) if snap['bids'].get(x)][::-1]}
        self.now_getting = ''
        self.getting_ob.clear()
        return ob

    @try_exc_regular
    def run_updater(self):
        self.wst_public.daemon = True
        self.wst_public.start()


if __name__ == '__main__':
    import configparser

    config = configparser.ConfigParser()
    config.read('config.ini', "utf-8")
    client = WhiteBitClient(keys=config['WHITEBIT'],
                            leverage=float(config['SETTINGS']['LEVERAGE']),
                            max_pos_part=int(config['SETTINGS']['PERCENT_PER_MARKET']))


    async def test_order():
        async with aiohttp.ClientSession() as session:
            ob = await client.get_orderbook_by_symbol('BTC_PERP')
            print(ob)
            price = ob['bids'][5][0]
            client.amount = client.instruments['BTC_PERP']['min_size']
            client.price = price
            data = await client.create_order('BTC_PERP', 'buy', session)
            print('CREATE_ORDER RESPONSE:', data)
            # print('GET ORDER_BY_ID RESPONSE:', client.get_order_by_id(data['exchange_order_id']))
            # time.sleep(1)
            # client.cancel_all_orders()
            # print('CANCEL_ALL_ORDERS RESPONSE:', data_cancel)


    client.run_updater()
    time.sleep(1)
    client.get_real_balance()
    client.get_position()
    client.cancel_all_orders()
    client.get_ws_token()
    # client.get_real_balance()
    # print(client.get_positions())
    # time.sleep(2)

    # asyncio.run(test_order())
    # print(len(client.get_markets()))
    while True:
        time.sleep(5)
        # print(client.get_all_tops())
