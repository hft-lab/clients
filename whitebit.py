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
import requests_cache
requests_cache.install_cache('dns_cache', backend='sqlite', expire_after=300)

from clients.core.enums import ResponseStatus, OrderStatus
from core.wrappers import try_exc_regular, try_exc_async
from clients.core.base_client import BaseClient


class WhiteBitClient(BaseClient):
    PUBLIC_WS_ENDPOINT = 'wss://api.whitebit.com/ws'
    BASE_URL = 'https://whitebit.com'
    EXCHANGE_NAME = 'WHITEBIT'

    def __init__(self, keys=None, leverage=None, markets_list=[], max_pos_part=20, finder=None):
        super().__init__()
        if finder:
            self.finder = finder
        if keys:
            self.api_key = keys['API_KEY']
            self.api_secret = keys['API_SECRET']
        self.headers = {"Accept": "application/json;charset=UTF-8",
                        "Content-Type": "application/json",
                        'User-Agent': 'python-whitebit-sdk',
                        'Connection': 'keep-alive'}
        self.markets_list = markets_list
        self.session = requests.session()
        self.session.headers.update(self.headers)
        self.instruments = {}
        self.leverage = leverage
        self.max_pos_part = max_pos_part
        self.price = 0
        self.amount = 0
        self.error_info = None
        self.markets = self.get_markets()
        self._loop = asyncio.new_event_loop()
        self._loop_supersonic = asyncio.new_event_loop()
        self._connected = asyncio.Event()
        self._wst_ = threading.Thread(target=self._run_ws_forever, args=[self._loop])
        self._wst_orderbooks = threading.Thread(target=self._process_ws_line)
        # self._extra_speed = threading.Thread(target=self.run_super_sonic)
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
        self.message_queue = asyncio.Queue(loop=self._loop)
        self.taker_fee = 0.00035

    @try_exc_regular
    def get_markets(self):
        path = "/api/v4/public/markets"
        resp = self.session.get(url=self.BASE_URL + path).json()
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
        params = self.get_auth_for_request(params, path)
        path += self._create_uri(params)
        res = self.session.post(url=self.BASE_URL + path, json=params)
        return res.json()

    @try_exc_regular
    def cancel_all_orders(self):
        path = '/api/v4/order/cancel/all'
        params = self.get_auth_for_request({}, path)
        path += self._create_uri(params)
        res = self.session.post(url=self.BASE_URL + path, json=params)
        return res.json()

    @try_exc_regular
    def get_position(self):
        path = "/api/v4/collateral-account/positions/open"
        params = self.get_auth_for_request({}, path)
        path += self._create_uri(params)
        res = self.session.post(url=self.BASE_URL + path, json=params)
        response = res.json()
        # print('GET_POSITION RESPONSE', response)
        self.positions = {}
        for pos in response:
            market = pos['market']
            ob = self.get_orderbook(market)
            if not ob:
                ob = self.get_orderbook_http_reg(market)
            change = (ob['asks'][0][0] + ob['bids'][0][0]) / 2
            self.positions.update({market: {'timestamp': int(datetime.utcnow().timestamp()),
                                            'entry_price': float(pos['basePrice']) if pos['basePrice'] else 0,
                                            'amount': float(pos['amount']),
                                            'amount_usd': change * float(pos['amount'])}})

        # print(self.EXCHANGE_NAME, 'POSITIONS AFTER UPDATE:', self.positions)

            # example = [{'positionId': 3634420, 'market': 'BTC_PERP', 'openDate': 1703664697.619855,
            #             'modifyDate': 1703664697.619855,
            #             'amount': '0.001', 'basePrice': '42523.8', 'liquidationPrice': '0', 'pnl': '0.2',
            #             'pnlPercent': '0.47',
            #             'margin': '8.6', 'freeMargin': '41.6', 'funding': '0', 'unrealizedFunding': '0',
            #             'liquidationState': None},
            #            {'positionId': 3642507, 'market': 'BTC_PERP', 'openDate': 1703752465.897688,
            #             'modifyDate': 1703752465.897688, 'amount': '0', 'basePrice': '', 'liquidationPrice': None,
            #             'pnl': None,
            #             'pnlPercent': None, 'margin': '8.7', 'freeMargin': '13.6', 'funding': '0',
            #             'unrealizedFunding': '0',
            #             'liquidationState': None}]
            # example_negative_pos = [
            #     {'positionId': 3635477, 'market': 'BTC_PERP', 'openDate': 1703677698.102418,
            #      'modifyDate': 1703677698.102418,
            #      'amount': '-0.002', 'basePrice': '43131.7', 'liquidationPrice': '66743.8', 'pnl': '0',
            #      'pnlPercent': '0.00',
            #      'margin': '17.3', 'freeMargin': '33.2', 'funding': '0', 'unrealizedFunding': '0',
            #      'liquidationState': None}]

    @try_exc_regular
    def get_real_balance(self):
        path = "/api/v4/collateral-account/balance"
        params = self.get_auth_for_request({}, path)
        path += self._create_uri(params)
        res = self.session.post(url=self.BASE_URL + path, json=params)
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
                        'bids': [[float(bid[0]), float(bid[1])] for bid in ob['bids']],
                        'timestamp': datetime.utcnow().timestamp(),
                        'top_ask_timestamp': datetime.utcnow().timestamp(),
                        'top_bid_timestamp': datetime.utcnow().timestamp()}
                    return orderbook

    @try_exc_regular
    def get_orderbook_http_reg(self, symbol):
        path = f'/api/v4/public/orderbook/{symbol}'
        params = {'limit': 10}  # Adjusting parameters as per WhiteBit's documentation
        path += self._create_uri(params)
        resp = self.session.get(url=self.BASE_URL + path)
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
        self.session.headers.update({
            'X-TXC-APIKEY': self.api_key,
            'X-TXC-SIGNATURE': signature,
            'X-TXC-PAYLOAD': payload.decode('ascii')
        })
        return params

    ### SUPERSONIC FEATURE ###
    @try_exc_async
    async def super_sonic_ob_update(self, market, coin, session):
        path = f'/api/v4/public/orderbook/{market}'
        params = {'limit': 1}  # Adjusting parameters as per WhiteBit's documentation
        path += self._create_uri(params)
        async with session.get(url=self.BASE_URL + path) as resp:
            ob = await resp.json()
            # print(ob)
            # Check if the response is a dictionary and has 'asks' and 'bids' directly within it
            if isinstance(ob, dict) and 'asks' in ob and 'bids' in ob:
                ask = ob['asks'][0]
                bid = ob['bids'][0]
                ts = ob['timestamp']
                old_top_ask = self.orderbook[market]
                old_top_bid = self.orderbook[market]
                top_ask_timestamp = ts if old_top_ask != ask else self.orderbook[market]['top_ask_timestamp']
                top_bid_timestamp = ts if old_top_bid != bid else self.orderbook[market]['top_bid_timestamp']
                orderbook = {
                    'asks': {ask[0]: ask[1]},
                    'bids': {bid[0]: bid[1]},
                    'top_ask': [float(ask[0]), float(ask[1])],
                    'top_bid': [float(bid[0]), float(bid[1])],
                    'timestamp': ts,
                    'top_ask_timestamp': top_ask_timestamp,
                    'top_bid_timestamp': top_bid_timestamp}
                # print(f"SuperSonic time: {time.time() - ts} sec")
                self.orderbook[market] = orderbook
                if top_bid_timestamp != ts or top_ask_timestamp != ts:
                    self.finder.coins_to_check.append(coin)
                    self.finder.update = True

    @try_exc_regular
    def find_best_market(self):
        tradable_markets = self.finder.tradable_profits.copy()
        min_profit_gap = 100
        target_coin = None
        for coin, exchanges in tradable_markets.items():
            if exchanges.get(self.EXCHANGE_NAME + 'BUY', 100) < min_profit_gap:
                min_profit_gap = exchanges[self.EXCHANGE_NAME + 'BUY']
                target_coin = coin
            if exchanges.get(self.EXCHANGE_NAME + 'SELL', 100) < min_profit_gap:
                min_profit_gap = exchanges[self.EXCHANGE_NAME + 'SELL']
                target_coin = coin
        return self.markets.get(target_coin), target_coin

    @try_exc_regular
    def run_super_sonic(self):
        self._loop_supersonic.run_until_complete(self._request_orderbooks())

    @try_exc_async
    async def _request_orderbooks(self):
        print('SUPERSONIC INITIALIZED')
        while not self.finder:
            time.sleep(1)
        async with aiohttp.ClientSession() as session:
            session.headers.update(self.headers)
            count = 0
            time_start = time.time()
            while True:
                # time.sleep(0.005)
                # print(f"SUPERSONIC GO!!!!")
                best_market, coin = self.find_best_market()
                if best_market:
                    await self._loop_supersonic.create_task(self.super_sonic_ob_update(best_market, coin, session))
                    count += 1
                    print(f"REAL REQUESTS FREQUENCY DATA:\nTIME: {time.time() - time_start}\nREQUESTS: {count}")
    ### SUPERSONIC FEATURE ###

    @try_exc_regular
    def _run_ws_forever(self, loop):
        while True:
            loop.run_until_complete(self._run_ws_loop())

    @try_exc_regular
    def _process_ws_line(self):
        self._loop.create_task(self.process_messages())

    @try_exc_async
    async def process_messages(self):
        while True:
            msg = await self.message_queue.get()
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
            self.message_queue.task_done()
            if self.message_queue.qsize() > 100:
                print(f'ALERT! {self.EXCHANGE_NAME} WS LINE LENGTH:', self.message_queue.qsize())

    @try_exc_regular
    def run_updater(self):
        self._wst_.daemon = True
        self._wst_.start()
        self._wst_orderbooks.daemon = True
        self._wst_orderbooks.start()
        # self._extra_speed.daemon = True
        # self._extra_speed.start()
        self.first_positions_update()

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
                for symbol in self.markets_list:
                    if market := self.markets.get(symbol):
                        await self._loop.create_task(self.subscribe_orderbooks(market))
                async for msg in ws:
                    await self.message_queue.put(msg)

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
        if not order:
            return OrderStatus.NOT_EXECUTED
        key = 'deal_stock' if order.get('deal_stock') else 'dealStock'
        rest = float(order['left']) if order.get('left') else float(order['amount']) - float(order['dealStock'])
        if status_id:
            if status_id == 1 and order[key] == '0':
                return OrderStatus.PROCESSING
            if status_id == 3 and order[key] == '0':
                return OrderStatus.NOT_EXECUTED
        if rest == 0:
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
    def get_order_by_id(self, symbol: str, order_id: int):
        path = '/api/v1/account/order_history'
        params = {'limit': 100}
        params = self.get_auth_for_request(params, path)
        path += self._create_uri(params)
        res = self.session.post(url=self.BASE_URL + path, json=params)
        response = res.json()
        # print(self.EXCHANGE_NAME, 'GET_ORDER_BY_ID STARTED')
        # print(self.EXCHANGE_NAME, 'GET_ORDER_BY_ID RESPONSE', response)
        right_order = {}
        factual_price = 0
        if response.get('success'):
            for market in response['result']:
                if not right_order:
                    for order in response['result'][market]:
                        if order['id'] == order_id:
                            right_order = order
                            # print(f"GET_ORDER_BY_ID ORDER FOUND: {right_order}")
                            if right_order['dealStock'] != '0':
                                factual_price = float(right_order['dealMoney']) / float(right_order['dealStock'])
                            break
                else:
                    break
        return {'exchange_order_id': order_id,
                'exchange': self.EXCHANGE_NAME,
                'status': self.get_order_status(right_order, 0),
                'factual_price': factual_price,
                'factual_amount_coin': float(right_order.get('dealStock', 0)),
                'factual_amount_usd': float(right_order.get('dealMoney', 0)),
                'datetime_update': datetime.utcnow(),
                'ts_update': right_order.get('ftime', datetime.utcnow().timestamp())}
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
        time_start = time.time()
        path = "/api/v4/order/collateral/limit"
        params = {"market": symbol,
                  "side": side,
                  "amount": self.amount,
                  "price": self.price}
        print(f"{self.EXCHANGE_NAME} SENDING ORDER: {params}")
        # if client_id:
        #     params['clientOrderId'] = client_id
        params = self.get_auth_for_request(params, path)
        path += self._create_uri(params)
        async with session.post(url=self.BASE_URL + path, headers=self.session.headers, json=params) as resp:
            response = await resp.json()
        # resp = self.session.post(url=self.BASE_URL + path, json=params)
        # response = resp.json()
            print(f"{self.EXCHANGE_NAME} ORDER CREATE RESPONSE: {response}")
            print(f"ORDER PLACING TIME: {time.time() - time_start}")
            # self.aver_time.append(time.time() - time_start)
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
        params = self.get_auth_for_request({}, path)
        path += self._create_uri(params)
        res = self.session.post(url=self.BASE_URL + path, json=params).json()
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
            orderbook = self.orderbook.get(symbol, {})
            if orderbook and orderbook.get('bids') and orderbook.get('asks'):
                tops.update({self.EXCHANGE_NAME + '__' + coin: {
                    'top_bid': orderbook['top_bid'][0], 'top_ask': orderbook['top_ask'][0],
                    'bid_vol': orderbook['top_bid'][1], 'ask_vol': orderbook['top_ask'][1],
                    'ts_exchange': orderbook['timestamp']}})
        return tops

    @try_exc_regular
    def update_orderbook(self, data):
        flag = False
        if data['params'][2] == self.now_getting:
            while self.getting_ob.is_set():
                time.sleep(0.00001)
        symbol = data['params'][2]
        for new_bid in data['params'][1].get('bids', []):
            if float(new_bid[0]) >= self.orderbook[symbol]['top_bid'][0]:
                self.orderbook[symbol]['top_bid'] = [float(new_bid[0]), float(new_bid[1])]
                self.orderbook[symbol]['top_bid_timestamp'] = data['params'][1]['timestamp']
                flag = True
            if self.orderbook[symbol]['bids'].get(new_bid[0]) and new_bid[1] == '0':
                del self.orderbook[symbol]['bids'][new_bid[0]]
                if float(new_bid[0]) == self.orderbook[symbol]['top_bid'][0] and len(self.orderbook[symbol]['bids']):
                    top = sorted(self.orderbook[symbol]['bids'])[-1]
                    self.orderbook[symbol]['top_bid'] = [float(top), float(self.orderbook[symbol]['bids'][top])]
                    self.orderbook[symbol]['top_bid_timestamp'] = data['params'][1]['timestamp']
            else:
                self.orderbook[symbol]['bids'][new_bid[0]] = new_bid[1]
        for new_ask in data['params'][1].get('asks', []):
            if float(new_ask[0]) <= self.orderbook[symbol]['top_ask'][0]:
                self.orderbook[symbol]['top_ask'] = [float(new_ask[0]), float(new_ask[1])]
                self.orderbook[symbol]['top_ask_timestamp'] = data['params'][1]['timestamp']
                flag = True
            if self.orderbook[symbol]['asks'].get(new_ask[0]) and new_ask[1] == '0':
                del self.orderbook[symbol]['asks'][new_ask[0]]
                if float(new_ask[0]) == self.orderbook[symbol]['top_ask'][0] and len(self.orderbook[symbol]['asks']):
                    top = sorted(self.orderbook[symbol]['asks'])[0]
                    self.orderbook[symbol]['top_ask'] = [float(top), float(self.orderbook[symbol]['asks'][top])]
                    self.orderbook[symbol]['top_ask_timestamp'] = data['params'][1]['timestamp']
            else:
                self.orderbook[symbol]['asks'][new_ask[0]] = new_ask[1]
        self.orderbook[symbol]['timestamp'] = data['params'][1]['timestamp']
        if flag and self.message_queue.qsize() <= 1:
            coin = symbol.split('_')[0]
            self.finder.coins_to_check.append(coin)
            self.finder.update = True

    @try_exc_regular
    def update_orderbook_snapshot(self, data):
        if data['params'][2] == self.now_getting:
            while self.getting_ob.is_set():
                time.sleep(0.00001)
        symbol = data['params'][2]
        ob = data['params'][1]
        if ob.get('asks') and ob.get('bids'):
            self.orderbook[symbol] = {'asks': {x[0]: x[1] for x in ob['asks']},
                                      'bids': {x[0]: x[1] for x in ob['bids']},
                                      'timestamp': data['params'][1]['timestamp'],
                                      'top_ask': [float(ob['asks'][0][0]), float(ob['asks'][0][1])],
                                      'top_bid': [float(ob['bids'][0][0]), float(ob['bids'][0][1])],
                                      'top_ask_timestamp': data['params'][1]['timestamp'],
                                      'top_bid_timestamp': data['params'][1]['timestamp']}

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
              'asks': [[float(x), float(snap['asks'][x])] for x in sorted(snap['asks'])[:5]],
              'bids': [[float(x), float(snap['bids'][x])] for x in sorted(snap['bids'])[-5:]][::-1],
              'top_ask_timestamp': self.orderbook[symbol]['top_ask_timestamp'],
              'top_bid_timestamp': self.orderbook[symbol]['top_bid_timestamp']}
        self.now_getting = ''
        self.getting_ob.clear()
        return ob

    @try_exc_regular
    def first_positions_update(self):
        while set(self.orderbook.keys()) < set([self.markets[x] for x in self.markets_list if self.markets.get(x)]):
            time.sleep(0.01)
        print('GOT ALL MARKETS')
        self.get_position()


if __name__ == '__main__':
    import configparser

    config = configparser.ConfigParser()
    config.read('config.ini', "utf-8")
    client = WhiteBitClient(keys=config['WHITEBIT'],
                            leverage=float(config['SETTINGS']['LEVERAGE']),
                            max_pos_part=int(config['SETTINGS']['PERCENT_PER_MARKET']),
                            markets_list=['BTC'])


    async def test_order():
        async with aiohttp.ClientSession() as session:
            ob = client.get_orderbook('BTC_PERP')
            loop = asyncio.get_event_loop()
            # price = ob['bids'][8][0]
            client.amount = client.instruments['BTC_PERP']['min_size']
            tasks = []
            time_start = time.time()
            for price in ob['bids'][3:6]:
                client.price = price[0]
                tasks.append(loop.create_task(client.create_order('BTC_PERP', 'buy', session)))
            data = await asyncio.gather(*tasks)
            print(f"ALL TIME: {time.time() - time_start} sec")
            print(data)
            # await client.create_order('BTC_PERP', 'buy', session)
            # print('CREATE_ORDER OUTPUT:', data)
            # print('GET ORDER_BY_ID OUTPUT:', client.get_order_by_id('asd', data['exchange_order_id']))
            time.sleep(1)
            client.cancel_all_orders()
            # print('CANCEL_ALL_ORDERS OUTPUT:', data_cancel)
            # print('GET ORDER_BY_ID OUTPUT AFTER CANCEL:', client.get_order_by_id('asd', data['exchange_order_id']))


    # id = 395769654408
    # print(client.get_order_by_id('abs', id))
    client.markets_list = list(client.markets.keys())
    client.run_updater()
    # time.sleep(1)
    # client.get_real_balance()
    # print('GET POSITION RESPONSE', client.get_position())
    # print(client.get_positions())
    # print(client.get_balance())
    # client.cancel_all_orders()
    # client.get_ws_token()
    # time.sleep(2)

    # client.get_position()
    # print(len(client.get_markets()))
    client.aver_time = []
    while True:
        time.sleep(5)
        for market in client.markets.values():
            print(client.get_orderbook(market))
            print()
        # print(client.get_all_tops())
        # asyncio.run(test_order())
        # print(f"Aver. order create time: {sum(client.aver_time) / len(client.aver_time)} sec")

        # print(client.markets)
        # for market in client.markets.values():
        #     print(market, client.get_orderbook(market))
        # print(client.get_all_tops())
