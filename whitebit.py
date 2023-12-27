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
        self.error_info = None
        self.markets = self.get_markets()
        self._loop = asyncio.new_event_loop()
        self._connected = asyncio.Event()
        self._wst_ = threading.Thread(target=self._run_ws_forever, args=[self._loop])
        self.requestLimit = 1200
        self.getting_ob = asyncio.Event()
        self.now_getting = ''
        self.orderbook = {}
        self.last_price = {}
        self.orders = {}
        self.balance = {}
        self.positions = {}
        self.websocket_token = self.get_ws_token()
        self.LAST_ORDER_ID = 'default'
        self.get_real_balance()
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
        # print('GET_POSITION RESPONSE', response)
        for pos in response:
            market = pos['market']
            ob = self.get_orderbook(market)
            if not ob:
                ob = self.get_orderbook_http_reg(market)
            change = (ob['asks'][0][0] + ob['bids'][0][0]) / 2
            self.positions.update({market: {'timestamp': int(datetime.utcnow().timestamp()),
                                            'entry_price': float(pos['basePrice']),
                                            'amount': float(pos['amount']),
                                            'amount_usd': change * float(pos['amount'])}})

    # example = [{'positionId': 3634420, 'market': 'BTC_PERP', 'openDate': 1703664697.619855,
    #             'modifyDate': 1703664697.619855,
    #             'amount': '0.001', 'basePrice': '42523.8', 'liquidationPrice': '0', 'pnl': '0.2', 'pnlPercent': '0.47',
    #             'margin': '8.6', 'freeMargin': '41.6', 'funding': '0', 'unrealizedFunding': '0',
    #             'liquidationState': None}]
    # example_negative_pos = [
    #     {'positionId': 3635477, 'market': 'BTC_PERP', 'openDate': 1703677698.102418, 'modifyDate': 1703677698.102418,
    #      'amount': '-0.002', 'basePrice': '43131.7', 'liquidationPrice': '66743.8', 'pnl': '0', 'pnlPercent': '0.00',
    #      'margin': '17.3', 'freeMargin': '33.2', 'funding': '0', 'unrealizedFunding': '0', 'liquidationState': None}]

    @try_exc_regular
    def get_real_balance(self):
        path = "/api/v4/collateral-account/balance"
        params, headers = self.get_auth_for_request({}, path)
        path += self._create_uri(params)
        res = requests.post(url=self.BASE_URL + path, headers=headers, json=params)
        response = res.json()
        # print('GET_REAL_BALANCE RESPONSE', response)
        self.balance = {'timestamp': datetime.utcnow().timestamp(),
                        'total': float(response['USDT']),
                        'free': float(response['USDT'])}
        # example = {'ADA': '0', 'APE': '0', 'ARB': '0', 'ATOM': '0', 'AVAX': '0', 'BCH': '0', 'BTC': '0', 'DOGE': '0',
        #            'DOT': '0', 'EOS': '0', 'ETC': '0', 'ETH': '0', 'LINK': '0', 'LTC': '0', 'MATIC': '0', 'NEAR': '0',
        #            'OP': '0', 'SHIB': '0', 'SOL': '0', 'TRX': '0', 'UNI': '0', 'USDC': '0', 'USDT': '50', 'WBT': '0',
        #            'XLM': '0', 'XRP': '0'}

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
    def get_orderbook_http_reg(self, symbol):
        path = f'/api/v4/public/orderbook/{symbol}'
        params = {'limit': 10}  # Adjusting parameters as per WhiteBit's documentation
        path += self._create_uri(params)
        resp = requests.get(url=self.BASE_URL + path, headers=self.headers)
        ob = resp.json()
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
        # print('BALANCES', data)
        self.balance = {'timestamp': datetime.utcnow().timestamp(),
                        'total': float(data['params'][0]['B']),
                        'free': float(data['params'][0]['av'])}
        # example = {'method': 'balanceMargin_update',
        #            'params': [{'a': 'USDT', 'B': '50', 'b': '0', 'av': '41.4684437', 'ab': '41.4684437'}], 'id': None}

    @try_exc_regular
    def update_orders(self, data):
        # print('ORDERS', data)
        status_id = 0
        for order in data['params']:
            if isinstance(order, int):
                status_id = order
                continue
            if float(order['deal_stock']) != '0':
                factual_price = float(order['deal_money']) / float(order['deal_stock'])
                side = 'sell' if order['side'] == 1 else 'buy'
                self.last_price.update({side: factual_price})
                self.get_position()
            else:
                factual_price = 0
            result = {'exchange_order_id': order['id'],
                      'exchange': self.EXCHANGE_NAME,
                      'status': self.get_order_status(order, status_id),
                      'factual_price': factual_price,
                      'factual_amount_coin': float(order['deal_stock']),
                      'factual_amount_usd': float(order['deal_money']),
                      'datetime_update': datetime.utcnow(),
                      'ts_update': order['mtime']}
            self.orders.update({order['id']: result})
        # example = {'method': 'ordersExecuted_update', 'params': [
        #     {'id': 395248275015, 'market': 'BTC_PERP', 'type': 7, 'side': 2, 'post_only': False, 'ioc': False,
        #      'ctime': 1703664697.619855, 'mtime': 1703664697.619855, 'price': '42511.7', 'amount': '0.001',
        #      'taker_fee': '0.00035', 'maker_fee': '0.0001', 'left': '0', 'deal_stock': '0.001', 'deal_money': '42.509',
        #      'deal_fee': '0.01487815', 'client_order_id': ''}], 'id': None}

    @try_exc_regular
    def get_order_status(self, order, status_id):
        # api_statuses = {1: 'new_order',
        #                 2: 'update_order',
        #                 3: 'finish(executed or canceled)'}
        key = 'deal_stock' if order.get('deal_stock') else 'dealStock'
        if status_id:
            if status_id == 1 and order[key] == '0':
                return OrderStatus.PROCESSING
            if status_id == 3 and order[key] == '0':
                return OrderStatus.NOT_EXECUTED
        if order['left'] == '0':
            return OrderStatus.FULLY_EXECUTED
        elif order[key] != '0':
            return OrderStatus.PARTIALLY_EXECUTED
        elif order[key] == '0':
            return OrderStatus.NOT_EXECUTED

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
    def get_last_price(self, side):
        return self.last_price.get(side, 0)

    @try_exc_regular
    def get_positions(self):
        return self.positions

    @try_exc_regular
    def get_order_by_id(self, symbol, order_id):
        path = '/api/v1/account/order_history'
        params = {'limit': 100}
        params, headers = self.get_auth_for_request(params, path)
        path += self._create_uri(params)
        res = requests.post(url=self.BASE_URL + path, headers=headers, json=params)
        response = res.json()
        print(self.EXCHANGE_NAME, 'GET_ORDER_BY_ID RESPONSE', response)
        if response.get('success'):
            for market in response['result']:
                for order in response['result'][market]:
                    if order['id'] == order_id:
                        if order['dealStock'] != '0':
                            factual_price = float(order['dealMoney']) / float(order['dealStock'])
                        else:
                            factual_price = 0
                        return {'exchange_order_id': order['id'],
                                'exchange': self.EXCHANGE_NAME,
                                'status': self.get_order_status(order, 0),
                                'factual_price': factual_price,
                                'factual_amount_coin': float(order['dealStock']),
                                'factual_amount_usd': float(order['dealMoney']),
                                'datetime_update': datetime.utcnow(),
                                'ts_update': order['mtime']}
        else:
            print(response)
            return None

        # example = {'success': True, 'message': '', 'result': {'BTC_PERP': [
        #     {'amount': '0.001', 'price': '43192.7', 'type': 'margin_limit', 'id': 395373055942, 'clientOrderId': '',
        #      'side': 'buy', 'ctime': 1703673670.631547, 'takerFee': '0.00035', 'ftime': 1703673672.240763,
        #      'makerFee': '0.0001', 'dealFee': '0', 'dealStock': '0', 'dealMoney': '0', 'status': 2,
        #      'marketName': 'BTC_PERP'},
        #     {'amount': '0.001', 'price': '43142.3', 'type': 'margin_limit', 'id': 395371130816, 'clientOrderId': '',
        #      'side': 'buy', 'ctime': 1703673540.805917, 'takerFee': '0.00035', 'ftime': 1703673542.389305,
        #      'makerFee': '0.0001', 'dealFee': '0', 'dealStock': '0', 'dealMoney': '0', 'status': 2,
        #      'marketName': 'BTC_PERP'}]}}

    @try_exc_async
    async def create_order(self, symbol, side, session=None, expire=10000, client_id=None):
        path = "/api/v4/order/collateral/limit"
        params = {
            "market": symbol,
            "side": side,
            "amount": self.amount,
            "price": self.price,
            "postOnly": False,
            "ioc": False
        }
        print(f"{self.EXCHANGE_NAME} SENDING ORDER: {params}")
        # if client_id:
        #     params['clientOrderId'] = client_id
        params, headers = self.get_auth_for_request(params, path)
        path += self._create_uri(params)
        async with session.post(url=self.BASE_URL + path, headers=headers, json=params) as resp:
            response = await resp.json()
            print(f"{self.EXCHANGE_NAME} ORDER CREATE RESPONSE: {response}")
            status = self.get_order_response_status(response)
            self.LAST_ORDER_ID = response.get('orderId', 'default')
            return {'exchange_name': self.EXCHANGE_NAME,
                    'exchange_order_id': response.get('orderId'),
                    'timestamp': response.get('timestamp', datetime.utcnow().timestamp()),
                    'status': status}
            # example_executed = {'orderId': 395248275015, 'clientOrderId': '', 'market': 'BTC_PERP', 'side': 'buy',
            # 'type': 'margin limit',
            #  'timestamp': 1703664697.619855, 'dealMoney': '42.509', 'dealStock': '0.001', 'amount': '0.001',
            #  'takerFee': '0.00035', 'makerFee': '0.0001', 'left': '0', 'dealFee': '0.01487815', 'ioc': False,
            #  'postOnly': False, 'price': '42511.7'}
            # example_failed = {'code': 17, 'message': 'Inner validation failed',
            #                   'errors': {'amount': ['Not enough balance.']}}

    @try_exc_regular
    def get_order_response_status(self, response):
        if response.get('left'):
            return ResponseStatus.SUCCESS
        else:
            self.error_info = response
            return ResponseStatus.ERROR
            # status = ResponseStatus.NO_CONNECTION

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
        if isinstance(snap['asks'], list):
            return snap
        ob = {'timestamp': self.orderbook[symbol]['timestamp'],
              'asks': [[float(x), float(snap['asks'][x])] for x in sorted(snap['asks']) if snap['asks'].get(x)],
              'bids': [[float(x), float(snap['bids'][x])] for x in sorted(snap['bids']) if snap['bids'].get(x)][::-1]}
        self.now_getting = ''
        self.getting_ob.clear()
        return ob

    @try_exc_regular
    def run_updater(self):
        self._wst_.daemon = True
        self._wst_.start()
        self.first_positions_update()

    @try_exc_regular
    def first_positions_update(self):
        while set(self.orderbook.keys()) < set(self.markets.values()):
            time.sleep(0.0001)
        print('GOT ALL MARKETS')
        self.get_position()


if __name__ == '__main__':
    import configparser

    config = configparser.ConfigParser()
    config.read('config.ini', "utf-8")
    client = WhiteBitClient(keys=config['WHITEBIT'],
                            leverage=float(config['SETTINGS']['LEVERAGE']),
                            max_pos_part=int(config['SETTINGS']['PERCENT_PER_MARKET']))


    async def test_order():
        async with aiohttp.ClientSession() as session:
            ob = client.get_orderbook('BTC_PERP')
            print(ob)
            price = ob['bids'][5][0]
            client.amount = client.instruments['BTC_PERP']['min_size']
            client.price = price
            data = await client.create_order('BTC_PERP', 'buy', session)
            print('CREATE_ORDER OUTPUT:', data)
            print('GET ORDER_BY_ID OUTPUT:', client.get_order_by_id('asd', data['exchange_order_id']))
            time.sleep(1)
            client.get_position()
            data_cancel = client.cancel_all_orders()
            print('CANCEL_ALL_ORDERS OUTPUT:', data_cancel)
            print('GET ORDER_BY_ID OUTPUT AFTER CANCEL:', client.get_order_by_id('asd', data['exchange_order_id']))


    client.run_updater()
    # time.sleep(1)
    # client.get_real_balance()
    # print('GET POSITION RESPONSE', client.get_position())
    print(client.get_positions())
    # print(client.get_balance())
    # client.cancel_all_orders()
    # client.get_ws_token()
    time.sleep(2)

    asyncio.run(test_order())
    # print(len(client.get_markets()))
    while True:
        time.sleep(5)
        # print(client.markets)
        # for market in client.markets.values():
        #     print(market, client.get_orderbook(market))
        # print(client.get_all_tops())
