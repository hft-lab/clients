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


class BtseClient:
    PUBLIC_WS_ENDPOINT = 'wss://ws.btse.com/ws/oss/futures'
    PRIVATE_WS_ENDPOINT = 'wss://ws.btse.com/ws/futures'
    BASE_URL = f"https://api.btse.com/futures"
    EXCHANGE_NAME = 'BTSE'

    def __init__(self, keys=None, leverage=None, markets_list=[], max_pos_part=20):
        if keys:
            self.api_key = keys['API_KEY']
            self.api_secret = keys['API_SECRET']
        self.headers = {"Accept": "application/json;charset=UTF-8",
                        "Content-Type": "application/json"}
        self.instruments = {}
        self.markets = self.get_markets()
        self.balance = {}
        self.error_info = None
        self._loop_public = asyncio.new_event_loop()
        self._loop_private = asyncio.new_event_loop()
        self._connected = asyncio.Event()
        self.getting_ob = asyncio.Event()
        self.now_getting = ''
        self.wst_public = threading.Thread(target=self._run_ws_forever, args=['public', self._loop_public])
        self.wst_private = threading.Thread(target=self._run_ws_forever, args=['private', self._loop_private])
        self.price = 0
        self.amount = 0
        self.requestLimit = 1200
        self.orderbook = {}
        self.taker_fee = 0.0005

    @staticmethod
    @try_exc_regular
    def get_price_precision(tick_size):
        if '.' in str(tick_size):
            price_precision = len(str(tick_size).split('.')[1])
        elif '-' in str(tick_size):
            price_precision = int(str(tick_size).split('-')[1])
        else:
            price_precision = 0
        return price_precision

    @try_exc_regular
    def get_markets(self):
        way = "https://api.btse.com/futures/api/v2.1/market_summary"
        resp = requests.get(url=way, headers=self.headers).json()
        markets = {}
        for market in resp:
            if market['active'] and 'PFC' in market['symbol']:
                markets.update({market['base']: market['symbol']})
                price_precision = self.get_price_precision(market['minPriceIncrement'])
                step_size = market['minSizeIncrement'] * market['contractSize']
                quantity_precision = len(str(step_size).split('.')[1]) if '.' in str(step_size) else 1
                self.instruments.update({market['symbol']: {'contract_value': market['contractSize'],
                                                            'tick_size': market['minPriceIncrement'],
                                                            'step_size': step_size,
                                                            'quantity_precision': quantity_precision,
                                                            'price_precision': price_precision,
                                                            'minsize': market['minOrderSize'] * market[
                                                                'contractSize']}})
        return markets

    @try_exc_regular
    def get_all_tops(self):
        tops = {}
        for coin, symbol in self.markets.items():
            orderbook = self.get_orderbook(symbol)
            if orderbook and orderbook['bids'] and orderbook['asks']:
                tops.update({self.EXCHANGE_NAME + '__' + coin: {
                    'top_bid': orderbook['bids'][0][0], 'top_ask': orderbook['asks'][0][0],
                    'bid_vol': orderbook['bids'][0][1], 'ask_vol': orderbook['asks'][0][1],
                    'ts_exchange': orderbook['timestamp']}})
        return tops

    @try_exc_regular
    def _run_ws_forever(self, type, loop):
        while True:
            loop.run_until_complete(self._run_ws_loop(type))

    @try_exc_regular
    def generate_signature(self, path, nonce, data=''):
        language = "latin-1"
        message = path + nonce + data
        signature = hmac.new(bytes(self.api_secret, language),
                             msg=bytes(message, language),
                             digestmod=hashlib.sha384).hexdigest()
        return signature

    @try_exc_regular
    def get_private_headers(self, path, data=''):
        json_data = json.dumps(data) if data else ''
        nonce = str(int(time.time() * 1000))
        signature = self.generate_signature(path, nonce, json_data)
        return {"request-api": self.api_key,
                "request-nonce": nonce,
                "request-sign": signature,
                "Accept": "application/json;charset=UTF-8",
                "Content-Type": "application/json"}

    @try_exc_regular
    def get_real_balance(self):
        path = '/api/v2.1/user/wallet'
        headers = self.get_private_headers(path)
        url = self.BASE_URL + path
        response = requests.get(url, headers=headers)
        balance_data = response.json()
        self.balance = {'timestamp': round(datetime.utcnow().timestamp()),
                        'total': balance_data[0]['totalValue'],
                        'free': balance_data[0]['availableBalance']}

    @try_exc_regular
    def cancel_all_orders(self):
        path = "/api/v2.1/order/cancelAllAfter"
        data = {"timeout": 10}
        headers = self.get_private_headers(path, data)
        response = requests.post(self.BASE_URL + path, headers=headers, json=data)
        return response

    @try_exc_regular
    def get_order_by_id(self, order_id):
        url = f"https://api.btse.com/futures/api/v2.1/order/{order_id}"
        response = requests.get(url, headers=self.headers)
        return response.json()

    @try_exc_regular
    def get_position(self):
        url = "https://api.btse.com/futures/api/v2.1/positions"
        response = requests.get(url, headers=self.headers)
        return response.json()

    @try_exc_regular
    def get_order_response_status(self, response):
        statuses = {2: 'Order Inserted',
                    3: 'Order Transacted',
                    4: 'Order Fully Transacted',
                    5: 'Order Partially Transacted',
                    6: 'Order Cancelled',
                    7: 'Order Refunded',
                    15: 'Order Rejected',
                    16: 'Order Not Found',
                    17: 'Request failed'}
        api_resp = statuses[response[0]['status']]
        timestamp = 0000000000000
        if api_resp in ['Order Refunded', 'Order Rejected', 'Order Not Found', 'Request failed']:
            status = ResponseStatus.ERROR
            self.error_info = response
        elif api_resp in ['Order Inserted', 'Order Transacted', 'Order Fully Transacted', 'Order Partially Transacted']:
            status = ResponseStatus.SUCCESS
            timestamp = response[0]['timestamp']
        else:
            status = ResponseStatus.NO_CONNECTION
        return status, timestamp

    @try_exc_regular
    async def create_order(self, symbol, side, session=None, expire=10000, client_id=None, expiration=None):
        path = '/api/v2.1/order'
        body = {"symbol": symbol,
                "side": side.upper(),
                "price": self.price,
                "type": "LIMIT",
                'size': self.amount}
        headers = self.get_private_headers(path, body)
        async with session.post(url=self.BASE_URL + path, headers=headers, json=body) as resp:
            res = await resp.json()
            status, timestamp = self.get_order_response_status(res)
            return {'exchange_name': self.EXCHANGE_NAME,
                    'exchange_order_id': res[0]['orderID'],
                    'timestamp': timestamp,
                    'status': status}

            # res_example = [{'status': 2, 'symbol': 'BTCPFC', 'orderType': 76, 'price': 43490, 'side': 'BUY', 'size': 1,
            #             'orderID': '13a82711-f6e2-4228-bf9f-3755cd8d7885', 'timestamp': 1703535543583,
            #             'triggerPrice': 0, 'trigger': False, 'deviation': 100, 'stealth': 100, 'message': '',
            #             'avgFillPrice': 0, 'fillSize': 0, 'clOrderID': '', 'originalSize': 1, 'postOnly': False,
            #             'remainingSize': 1, 'orderDetailType': None, 'positionMode': 'ONE_WAY',
            #             'positionDirection': None, 'positionId': 'BTCPFC-USD', 'time_in_force': 'GTC'}]

    @try_exc_async
    async def _run_ws_loop(self, type):
        async with aiohttp.ClientSession() as s:
            if type == 'private':
                endpoint = self.PRIVATE_WS_ENDPOINT
            else:
                endpoint = self.PUBLIC_WS_ENDPOINT
            async with s.ws_connect(endpoint) as ws:
                print(f"BTSE: connected {type}")
                self._connected.set()
                # try:
                if type == 'private':
                    self._ws_private = ws
                    await self._loop_private.create_task(self.subscribe_privates())
                else:
                    self._ws_public = ws
                    await self._loop_public.create_task(self.subscribe_orderbooks())
                async for msg in ws:
                    data = json.loads(msg.data)
                    if type == 'public':
                        if data.get('data') and data['data']['type'] == 'snapshot':
                            if data['data']['symbol'] == self.now_getting:
                                while self.getting_ob.is_set():
                                    time.sleep(0.00001)
                            self.update_orderbook_snapshot(data)
                        elif data.get('data') and data['data']['type'] == 'delta':
                            if data['data']['symbol'] == self.now_getting:
                                while self.getting_ob.is_set():
                                    time.sleep(0.00001)
                            self.update_orderbook(data)
                    elif type == 'private':
                        print(data)

    @try_exc_async
    async def subscribe_orderbooks(self):
        args = [f"update:{x}_0" for x in self.markets.values()]
        method = {"op": "subscribe",
                  "args": args}
        await self._connected.wait()
        await self._ws_public.send_json(method)

    def get_wss_auth(self):
        url = "/ws/futures"
        headers = self.get_private_headers(url)
        data = {"op": "authKeyExpires",
                "args": [headers["request-api"],
                         headers["request-nonce"],
                         headers["request-sign"]]}
        return data

    async def subscribe_privates(self):
        method_pos = {"op": "subscribe",
                      "args": ["allPosition"]}
        method_fills = {"op": "subscribe",
                        "args": ["fills"]}
        auth = self.get_wss_auth()
        await self._connected.wait()
        await self._ws_private.send_json(auth)
        await self._ws_private.send_json(method_pos)
        await self._ws_private.send_json(method_fills)

    @try_exc_regular
    def update_orderbook(self, data):
        symbol = data['data']['symbol']
        for new_bid in data['data']['bids']:
            res = self.orderbook[symbol]['bids']
            if res.get(new_bid[0]) and new_bid[1] == '0':
                del res[new_bid[0]]
            else:
                self.orderbook[symbol]['bids'][new_bid[0]] = new_bid[1]
        for new_ask in data['data']['asks']:
            res = self.orderbook[symbol]['asks']
            if res.get(new_ask[0]) and new_ask[1] == '0':
                del res[new_ask[0]]
            else:
                self.orderbook[symbol]['asks'][new_ask[0]] = new_ask[1]
        self.orderbook[symbol]['timestamp'] = data['data']['timestamp']

    @try_exc_regular
    def update_orderbook_snapshot(self, data):
        symbol = data['data']['symbol']
        self.orderbook[symbol] = {'asks': {x[0]: x[1] for x in data['data']['asks']},
                                  'bids': {x[0]: x[1] for x in data['data']['bids']},
                                  'timestamp': data['data']['timestamp']}

    @try_exc_regular
    def get_orderbook(self, symbol) -> dict:
        if not self.orderbook.get(symbol):
            return {}
        self.getting_ob.set()
        self.now_getting = symbol
        snap = self.orderbook[symbol]
        c_v = self.instruments[symbol]['contract_value']
        ob = {'timestamp': self.orderbook[symbol]['timestamp'],
              'asks': [[float(x), float(snap['asks'][x]) * c_v] for x in sorted(snap['asks']) if snap['asks'].get(x)],
              'bids': [[float(x), float(snap['bids'][x]) * c_v] for x in sorted(snap['bids']) if snap['bids'].get(x)][
                      ::-1]}
        self.now_getting = ''
        self.getting_ob.clear()
        return ob

    async def get_orderbook_by_symbol(self, symbol, group=None, limit_bids=10, limit_asks=10):
        async with aiohttp.ClientSession() as session:
            path = f'/api/v2.1/orderbook'
            params = {'symbol': symbol, 'group': group, 'limit_bids': limit_bids, 'limit_asks': limit_asks}
            # headers = self.get_private_headers(path, {})  # Assuming authentication is required
            async with session.get(url=self.BASE_URL + path, headers=self.headers, json=params) as resp:
                ob = await resp.json()
                contract_value = self.instruments[symbol]['contract_value']
                if 'buyQuote' in ob and 'sellQuote' in ob:
                    orderbook = {
                        'timestamp': ob['timestamp'],
                        'asks': [[float(ask['price']), float(ask['size']) * contract_value] for ask in ob['sellQuote']],
                        'bids': [[float(bid['price']), float(bid['size']) * contract_value] for bid in ob['buyQuote']]
                    }
                    return orderbook

    @try_exc_regular
    def run_updater(self):
        self.wst_public.daemon = True
        self.wst_public.start()
        self.wst_private.daemon = True
        self.wst_private.start()


if __name__ == '__main__':
    import configparser

    config = configparser.ConfigParser()
    config.read('config.ini', "utf-8")
    client = BtseClient(keys=config['BTSE'],
                        leverage=float(config['SETTINGS']['LEVERAGE']),
                        max_pos_part=int(config['SETTINGS']['PERCENT_PER_MARKET']))


    async def test_order():
        async with aiohttp.ClientSession() as session:
            ob = await client.get_orderbook_by_symbol('BTCPFC')
            price = ob['bids'][5][0]
            client.amount = 1
            client.price = price
            data = await client.create_order('BTCPFC', 'buy', session)
            print(data)
            data_cancel = client.cancel_all_orders()
            print(data_cancel)


    client.run_updater()
    client.get_real_balance()
    print(client.balance)

    # client.futures_place_limit_order({"symbol": 'BTCPFC',
    #                                   "side": 'buy'.upper(),
    #                                   "price": client.price,
    #                                   "type": "LIMIT",
    #                                   'size': client.amount})
    # print(client.create_order('BTCPFC', 'buy'))
    # print(client.cancel_all_orders())
    time.sleep(3)
    asyncio.run(test_order())
    while True:
        time.sleep(5)
        # for symbol in client.orderbook.keys():
        #     print(symbol, client.get_orderbook(symbol), '\n')
        # print(client.get_all_tops())
