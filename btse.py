import time
import aiohttp
import json
import requests
from datetime import datetime
import asyncio
import threading
import hmac
import hashlib

from clients.core.enums import ResponseStatus, OrderStatus, ClientsOrderStatuses
from core.wrappers import try_exc_regular, try_exc_async
from clients.core.base_client import BaseClient


class BtseClient(BaseClient):
    PUBLIC_WS_ENDPOINT = 'wss://ws.btse.com/ws/oss/futures'
    PRIVATE_WS_ENDPOINT = 'wss://ws.btse.com/ws/futures'
    BASE_URL = f"https://api.btse.com/futures"
    EXCHANGE_NAME = 'BTSE'
    order_statuses = {2: 'Order Inserted',
                      3: 'Order Transacted',
                      4: 'Order Fully Transacted',
                      5: 'Order Partially Transacted',
                      6: 'Order Cancelled',
                      7: 'Order Refunded',
                      9: 'Trigger Inserted',
                      10: 'Trigger Activated',
                      15: 'Order Rejected',
                      16: 'Order Not Found',
                      17: 'Request failed'}

    def __init__(self, keys=None, leverage=None, markets_list=[], max_pos_part=20):
        super().__init__()
        self.max_pos_part = max_pos_part
        self.leverage = leverage
        if keys:
            self.api_key = keys['API_KEY']
            self.api_secret = keys['API_SECRET']
        self.headers = {"Accept": "application/json;charset=UTF-8",
                        "Content-Type": "application/json"}
        self.positions = {}
        self.instruments = {}
        self.markets = self.get_markets()
        self.balance = {}
        self.get_real_balance()
        self.get_position()
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
        self.orders = {}
        self.last_price = {}
        self.taker_fee = 0.0005
        self.orig_sizes = {}

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
                min_size = market['minOrderSize'] * market['contractSize']
                self.instruments.update({market['symbol']: {'contract_value': market['contractSize'],
                                                            'tick_size': market['minPriceIncrement'],
                                                            'step_size': step_size,
                                                            'quantity_precision': quantity_precision,
                                                            'price_precision': price_precision,
                                                            'min_size': min_size}})
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
        if response.status_code in ['200', 200, '201', 201]:
            balance_data = response.json()
            self.balance = {'timestamp': round(datetime.utcnow().timestamp()),
                            'total': balance_data[0]['totalValue'],
                            'free': balance_data[0]['availableBalance']}
        else:
            print(response.text)

    @try_exc_regular
    def cancel_all_orders(self):
        path = "/api/v2.1/order/cancelAllAfter"
        data = {"timeout": 10}
        headers = self.get_private_headers(path, data)
        response = requests.post(self.BASE_URL + path, headers=headers, json=data)
        return response.text

    @try_exc_regular
    def get_position(self):
        path = "/api/v2.1/user/positions"
        headers = self.get_private_headers(path)
        response = requests.get(self.BASE_URL + path, headers=headers)
        if response.status_code in ['200', 200, '201', 201]:
            for pos in response.json():
                contract_value = self.instruments[pos['symbol']]['contract_value']
                if pos['side'] == 'BUY':
                    size_usd = pos['orderValue']
                    size_coin = pos['size'] * contract_value
                else:
                    size_usd = -pos['orderValue']
                    size_coin = -pos['size'] * contract_value
                self.positions.update({pos['symbol']: {'timestamp': int(datetime.utcnow().timestamp()),
                                                       'entry_price': pos['entryPrice'],
                                                       'amount': size_coin,
                                                       'amount_usd': size_usd}})
        else:
            print(response.text)

    @try_exc_regular
    def get_order_response_status(self, response):
        api_resp = self.order_statuses.get(response[0]['status'], None)
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
    async def create_order(self, symbol, side, session=None, expire=10000, client_id=None, expiration=None):
        path = '/api/v2.1/order'
        contract_value = self.instruments[symbol]['contract_value']
        body = {"symbol": symbol,
                "side": side.upper(),
                "price": self.price,
                "type": "LIMIT",
                'size': int(self.amount / contract_value)}
        headers = self.get_private_headers(path, body)
        async with session.post(url=self.BASE_URL + path, headers=headers, json=body) as resp:
            res = await resp.json()
            print(f"{self.EXCHANGE_NAME} ORDER CREATE RESPONSE: {res}")
            status, timestamp = self.get_order_response_status(res)
            self.orig_sizes.update({res[0]['orderID']: res[0]['originalSize']})
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

    @try_exc_regular
    def get_status_of_order(self, stat_num):
        if api_status := self.order_statuses.get(stat_num):
            if api_status == 'Order Fully Transacted':
                return OrderStatus.FULLY_EXECUTED
            elif api_status in ['Order Transacted', 'Order Partially Transacted']:
                return OrderStatus.PARTIALLY_EXECUTED
            elif api_status == 'Order Inserted':
                return OrderStatus.PROCESSING
            else:
                return OrderStatus.NOT_EXECUTED

    @try_exc_regular
    def get_order_by_id(self, order_id=None, cl_order_id=None):
        if order_id is None and cl_order_id is None:
            raise ValueError("Either orderID or clOrderID must be provided")
        params = {}
        path = "/api/v2.1/order"
        if order_id:
            params['orderID'] = order_id
            final_path = f"/api/v2.1/order?orderID={order_id}"
        else:
            params['clOrderID'] = cl_order_id
            final_path = f"/api/v2.1/order?clOrderID={cl_order_id}"
        headers = self.get_private_headers(path)
        response = requests.get(url=self.BASE_URL + final_path, headers=headers)
        if response.status_code in ['200', 200, '201', 201]:
            order_data = response.json()
        else:
            print(response.text)
        symbol = order_data.get('symbol').replace('-PERP', 'PFC')
        c_v = self.instruments[symbol]['contract_value']
        return {'exchange_order_id': order_data.get('orderID'),
                'exchange': self.EXCHANGE_NAME,
                'status': self.get_status_of_order(order_data.get('status', 0)),
                'factual_price': order_data.get('avgFilledPrice'),
                'factual_amount_coin': order_data.get('filledSize', 0) * c_v,
                'factual_amount_usd': order_data.get('filledSize', 0) * c_v * order_data.get('avgFilledPrice', 0),
                'datetime_update': datetime.utcnow(),
                'ts_update': int(datetime.utcnow().timestamp() * 1000)}
        # get_order_example = {'orderType': 76, 'price': 42424.8, 'size': 1, 'side': 'BUY', 'filledSize': 1,
        #                      'orderValue': 424.248, 'pegPriceMin': 0, 'pegPriceMax': 0, 'pegPriceDeviation': 1,
        #                      'timestamp': 1703584934116, 'orderID': 'fab9af76-8c8f-4fd3-b006-d7da8557d462',
        #                      'stealth': 1, 'triggerOrder': False, 'triggered': False, 'triggerPrice': 0,
        #                      'triggerOriginalPrice': 0, 'triggerOrderType': 0, 'triggerTrailingStopDeviation': 0,
        #                      'triggerStopPrice': 0, 'symbol': 'ETH-PERP', 'trailValue': 0, 'remainingSize': 0,
        #                      'clOrderID': '', 'reduceOnly': False, 'status': 4, 'triggerUseLastPrice': False,
        #                      'avgFilledPrice': 2224.66, 'timeInForce': 'GTC', 'closeOrder': False}

    @try_exc_regular
    def get_positions(self):
        # NECESSARY
        return self.positions

    @try_exc_regular
    def get_orders(self):
        # NECESSARY
        return self.orders

    @try_exc_regular
    def get_balance(self):
        return self.balance['total']

    @try_exc_regular
    def get_available_balance(self):
        return super().get_available_balance(
            leverage=self.leverage,
            max_pos_part=self.max_pos_part,
            positions=self.positions,
            balance=self.balance)

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
                            self.update_orderbook_snapshot(data)
                        elif data.get('data') and data['data']['type'] == 'delta':
                            self.update_orderbook(data)
                    elif type == 'private':
                        self.update_private_data(data)

    @try_exc_regular
    def update_private_data(self, data):
        if data.get('topic') == 'allPosition':
            self.update_positions(data)
        elif data.get('topic') == 'fills':
            self.update_fills(data)

    @try_exc_regular
    def get_order_status_by_fill(self, order_id, size):
        orig_size = self.orig_sizes.get(order_id)
        if orig_size == size:
            return OrderStatus.FULLY_EXECUTED
        else:
            return OrderStatus.PARTIALLY_EXECUTED

    @try_exc_regular
    def update_fills(self, data):
        for fill in data['data']:
            self.last_price.update({fill['side'].lower(): float(fill['price'])})
            order_id = fill['orderId']
            size = float(fill['size']) * self.instruments[fill['symbol']]['contract_value']
            size_usd = size * float(fill['price'])
            if order := self.orders.get(order_id):
                avg_price = (order['factual_amount_usd'] + size_usd) / (size + order['factual_amount_coin'])
                new_size = order['factual_amount_coin'] + size
                result_exist = {'status': self.get_order_status_by_fill(order_id, new_size),
                                'factual_price': avg_price,
                                'factual_amount_coin': new_size,
                                'factual_amount_usd': order['factual_amount_usd'] + size_usd,
                                'datetime_update': datetime.utcnow(),
                                'ts_update': fill['timestamp']}
                self.orders[order_id].update(result_exist)
                continue
            result_new = {'exchange_order_id': order_id,
                          'exchange': self.EXCHANGE_NAME,
                          'status': self.get_order_status_by_fill(order_id, size),
                          'factual_price': float(fill['price']),
                          'factual_amount_coin': size,
                          'factual_amount_usd': size_usd,
                          'datetime_update': datetime.utcnow(),
                          'ts_update': fill['timestamp']}
            self.orders.update({order_id: result_new})
        # fills_example = {'topic': 'fills', 'id': '', 'data': [
        #     {'orderId': '04e64f39-b715-44e5-a2b8-e60b3379c2f3', 'serialId': 4260246, 'clOrderId': '', 'type': '76',
        #      'symbol': 'ETHPFC', 'side': 'BUY', 'price': '2238.25', 'size': '1.0', 'feeAmount': '0.01119125',
        #      'feeCurrency': 'USDT', 'base': 'ETHPFC', 'quote': 'USD', 'maker': False, 'timestamp': 1703580743141,
        #      'tradeId': 'd6201931-446d-4a1c-ab83-e3ebdcf2f077'}]}

    @try_exc_regular
    def update_positions(self, data):
        for pos in data['data']:
            market = pos['marketName'].split('-')[0]
            c_v = self.instruments[market]['contract_value']
            self.positions.update({market: {'timestamp': int(datetime.utcnow().timestamp()),
                                            'entry_price': pos['entryPrice'],
                                            'amount': pos['totalContracts'] * c_v,
                                            'amount_usd': pos['totalValue']}})
        # positions_example = {'topic': 'allPosition', 'id': '', 'data': [
        #     {'id': 2343355483091732596, 'requestId': 0, 'username': 'nikicha', 'userCurrency': None,
        #      'marketName': 'BTCPFC-USD', 'orderType': 90, 'orderMode': 66, 'status': 65, 'originalAmount': 0.001,
        #      'maxPriceHeld': 0, 'pegPriceMin': 0, 'stealth': 1, 'baseCurrency': None, 'quoteCurrency': None,
        #      'quoteCurrencyFiat': False, 'parents': None, 'makerFeesRatio': None, 'takerFeesRatio': [0.0005],
        #      'ip': None, 'systemId': None, 'orderID': None, 'vendorName': None, 'botID': None, 'poolID': 0,
        #      'maxStealthDisplayAmount': 0, 'sellexchangeRate': 0, 'tag': None, 'triggerPrice': 0, 'closeOrder': False,
        #      'dbBaseBalHeld': 0, 'dbQuoteBalHeld': -0.567521753, 'isFuture': True, 'liquidationInProgress': False,
        #      'marginType': 91, 'entryPrice': 42758.3, 'liquidationPrice': 23153.9536897814,
        #      'markedPrice': 42600.160009819, 'marginHeld': 0, 'unrealizedProfitLoss': -0.15813999,
        #      'totalMaintenanceMargin': 0.243960466, 'totalContracts': 1, 'marginChargedLongOpen': 0,
        #      'marginChargedShortOpen': 0, 'unchargedMarginLongOpen': 0, 'unchargedMarginShortOpen': 0,
        #      'isolatedCurrency': None, 'isolatedLeverage': 0, 'totalFees': 0, 'totalValue': 42.60016001,
        #      'adlScoreBucket': 1, 'adlScorePercentile': 0.3870967742, 'booleanVar1': False, 'char1': '\x00',
        #      'orderTypeName': 'TYPE_FUTURES_POSITION', 'orderModeName': 'MODE_BUY',
        #      'marginTypeName': 'FUTURES_MARGIN_CROSS', 'currentLeverage': 4.4022854879, 'averageFillPrice': 0,
        #      'filledSize': 0, 'takeProfitOrder': None, 'stopLossOrder': None, 'positionId': 'BTCPFC-USD',
        #      'positionMode': 'ONE_WAY', 'positionDirection': None, 'future': True, 'settleWithNonUSDAsset': 'USDT'},
        #
        #     {'id': 5915202914346471829, 'requestId': 0, 'username': 'nikicha', 'userCurrency': None,
        #      'marketName': 'ETHPFC-USD', 'orderType': 90, 'orderMode': 66, 'status': 65, 'originalAmount': 0.01,
        #      'maxPriceHeld': 0, 'pegPriceMin': 0, 'stealth': 1, 'baseCurrency': None, 'quoteCurrency': None,
        #      'quoteCurrencyFiat': False, 'parents': None, 'makerFeesRatio': None, 'takerFeesRatio': [0.0005],
        #      'ip': None, 'systemId': None, 'orderID': None, 'vendorName': None, 'botID': None, 'poolID': 0,
        #      'maxStealthDisplayAmount': 0, 'sellexchangeRate': 0, 'tag': None, 'triggerPrice': 0, 'closeOrder': False,
        #      'dbBaseBalHeld': 0, 'dbQuoteBalHeld': -0.567521753, 'isFuture': True, 'liquidationInProgress': False,
        #      'marginType': 91, 'entryPrice': 2231.84, 'liquidationPrice': 1264.1365670193,
        #      'markedPrice': 2236.501887389, 'marginHeld': 0, 'unrealizedProfitLoss': 0.09323775,
        #      'totalMaintenanceMargin': 0.258659047, 'totalContracts': 2, 'marginChargedLongOpen': 0,
        #      'marginChargedShortOpen': 0, 'unchargedMarginLongOpen': 0, 'unchargedMarginShortOpen': 0,
        #      'isolatedCurrency': None, 'isolatedLeverage': 0, 'totalFees': 0, 'totalValue': 44.730037748,
        #      'adlScoreBucket': 2, 'adlScorePercentile': 0.7073170732, 'booleanVar1': False, 'char1': '\x00',
        #      'orderTypeName': 'TYPE_FUTURES_POSITION', 'orderModeName': 'MODE_BUY',
        #      'marginTypeName': 'FUTURES_MARGIN_CROSS', 'currentLeverage': 4.4022854879, 'averageFillPrice': 0,
        #      'filledSize': 0, 'takeProfitOrder': None, 'stopLossOrder': None, 'positionId': 'ETHPFC-USD',
        #      'positionMode': 'ONE_WAY', 'positionDirection': None, 'future': True, 'settleWithNonUSDAsset': 'USDT'}]}

    @try_exc_async
    async def subscribe_orderbooks(self):
        args = [f"update:{x}_0" for x in self.markets.values()]
        method = {"op": "subscribe",
                  "args": args}
        await self._connected.wait()
        await self._ws_public.send_json(method)

    @try_exc_regular
    def get_wss_auth(self):
        url = "/ws/futures"
        headers = self.get_private_headers(url)
        data = {"op": "authKeyExpires",
                "args": [headers["request-api"],
                         headers["request-nonce"],
                         headers["request-sign"]]}
        return data

    @try_exc_async
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
        if data['data']['symbol'] == self.now_getting:
            while self.getting_ob.is_set():
                time.sleep(0.00001)
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
        if data['data']['symbol'] == self.now_getting:
            while self.getting_ob.is_set():
                time.sleep(0.00001)
        symbol = data['data']['symbol']
        self.orderbook[symbol] = {'asks': {x[0]: x[1] for x in data['data']['asks']},
                                  'bids': {x[0]: x[1] for x in data['data']['bids']},
                                  'timestamp': data['data']['timestamp']}

    @try_exc_regular
    def get_last_price(self, side: str) -> float:
        return self.last_price[side]

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

    @try_exc_async
    async def get_orderbook_by_symbol(self, symbol):
        async with aiohttp.ClientSession() as session:
            path = f'/api/v2.1/orderbook'
            params = {'symbol': symbol, 'depth': 10}
            post_string = '?' + "&".join([f"{key}={params[key]}" for key in sorted(params)])
            # headers = self.get_private_headers(path, {})  # Assuming authentication is required
            async with session.get(url=self.BASE_URL + path + post_string, headers=self.headers, data=params) as resp:
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
            ob = await client.get_orderbook_by_symbol('ETHPFC')
            price = ob['bids'][5][0]
            client.amount = client.instruments['ETHPFC']['min_size']
            client.price = price
            data = await client.create_order('ETHPFC', 'buy', session)
            print('CREATE_ORDER RESPONSE:', data)
            print('GET ORDER_BY_ID RESPONSE:', client.get_order_by_id(data['exchange_order_id']))
            time.sleep(1)
            client.cancel_all_orders()
            time.sleep(1)

            print('GET ORDER_BY_ID RESPONSE:', client.get_order_by_id(data['exchange_order_id']))

            # print('CANCEL_ALL_ORDERS RESPONSE:', data_cancel)


    client.run_updater()
    time.sleep(1)
    # client.get_real_balance()
    print(client.get_positions())
    time.sleep(2)
    asyncio.run(test_order())
    client.get_position()
    while True:
        time.sleep(5)
        # print(client.get_all_tops())
