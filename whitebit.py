import time
import aiohttp
import json
import requests
from datetime import datetime
import asyncio
import threading
# from core.wrappers import try_exc_regular, try_exc_async
import hmac
import hashlib


class WhiteBitClient:
    PUBLIC_WS_ENDPOINT = 'wss://api.whitebit.com/ws'
    BASE_URL = 'https://whitebit.com'
    EXCHANGE_NAME = 'WHITEBIT'

    def __init__(self, keys=None, leverage=None, markets_list=[], max_pos_part=20):
        if keys:
            self.api_key = keys['API_KEY']
            self.api_secret = keys['API_SECRET']
        self.headers = {"Accept": "application/json;charset=UTF-8",
                        "Content-Type": "application/json"}
        self.instruments = {}
        self.markets = self.get_markets()
        self._loop_public = asyncio.new_event_loop()
        self._connected = asyncio.Event()
        self.wst_public = threading.Thread(target=self._run_ws_forever, args=[self._loop_public])
        self.requestLimit = 1200
        self.getting_ob = asyncio.Event()
        self.now_getting = ''
        self.orderbook = {}
        self.taker_fee = 0.00035

    # @try_exc_regular
    def get_markets(self):
        path = "/api/v4/public/markets"
        resp = requests.get(url=self.BASE_URL + path, headers=self.headers).json()
        markets = {}
        for market in resp:
            if market['type'] == 'futures' and market['tradesEnabled']:
                markets.update({market['stock']: market['name']})
                money_prec = int(market['moneyPrec'])
                stock_prec = int(market['stockPrec'])
                min_amount = float(market['minAmount'])
                tick_size = self.calculate_tick_size(money_prec)
                step_size = min_amount
                quantity_precision = stock_prec
                price_precision = money_prec
                self.instruments[market['name']] = {'tick_size': tick_size,
                                                    'step_size': step_size,
                                                    'quantity_precision': quantity_precision,
                                                    'price_precision': price_precision,
                                                    'min_size': min_amount}
        return markets

    @staticmethod
    def calculate_precision(precision):
        return max(len(str(precision).split('.')[-1]), 0) if '.' in str(precision) else 0

    @staticmethod
    def calculate_tick_size(precision):
        return float(f"1e-{precision}")

    # @try_exc_async
    async def get_orderbook_by_symbol(self, symbol):
        async with aiohttp.ClientSession() as session:
            path = f'/api/v4/public/orderbook/{symbol}'
            params = {'limit': 10}  # Adjusting parameters as per WhiteBit's documentation
            query_string = '?' + "&".join([f"{key}={params[key]}" for key in params])

            async with session.get(url=self.BASE_URL + path + query_string, headers=self.headers) as resp:
                ob = await resp.json()
                # Check if the response is a dictionary and has 'asks' and 'bids' directly within it
                if isinstance(ob, dict) and 'asks' in ob and 'bids' in ob:
                    orderbook = {
                        'asks': [[float(ask[0]), float(ask[1])] for ask in ob['asks']],
                        'bids': [[float(bid[0]), float(bid[1])] for bid in ob['bids']]
                    }
                    return orderbook

    #     @try_exc_regular
    def _run_ws_forever(self, loop):
        while True:
            loop.run_until_complete(self._run_ws_loop())

    #     @try_exc_async
    async def _run_ws_loop(self):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(self.PUBLIC_WS_ENDPOINT) as ws:
                self._connected.set()
                self._ws_public = ws
                for market in self.markets.values():
                    await self._loop_public.create_task(self.subscribe_orderbooks(market))
                async for msg in ws:
                    data = json.loads(msg.data)
                    if data.get('params'):
                        if data['params'][2] == self.now_getting:
                            while self.getting_ob.is_set():
                                time.sleep(0.00001)
                        if data['params'][0]:
                            self.update_orderbook_snapshot(data)
                        else:
                            self.update_orderbook(data)

    #     @try_exc_async
    async def subscribe_orderbooks(self, symbol):
        data = [symbol, 10, '0', True]
        method = {"id": 921841274981274,
                  "method": "depth_subscribe",
                  "params": data}
        await self._connected.wait()
        await self._ws_public.send_json(method)

        #     @try_exc_regular

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

    # @try_exc_regular
    def update_orderbook(self, data):
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

    # @try_exc_regular
    def update_orderbook_snapshot(self, data):
        symbol = data['params'][2]
        self.orderbook[symbol] = {'asks': {x[0]: x[1] for x in data['params'][1].get('asks', [])},
                                  'bids': {x[0]: x[1] for x in data['params'][1].get('bids', [])},
                                  'timestamp': data['params'][1]['timestamp']}

    # @try_exc_regular
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

    #     @try_exc_regular
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
            ob = await client.get_orderbook_by_symbol('BTC_USDT')
            print(ob)
            # price = ob['bids'][5][0]
            # client.amount = client.instruments['ETHPFC']['min_size']
            # client.price = price
            # data = await client.create_order('ETHPFC', 'buy', session)
            # print('CREATE_ORDER RESPONSE:', data)
            # print('GET ORDER_BY_ID RESPONSE:', client.get_order_by_id(data['exchange_order_id']))
            # time.sleep(1)
            # client.cancel_all_orders()
            # print('CANCEL_ALL_ORDERS RESPONSE:', data_cancel)


    # client.run_updater()
    time.sleep(1)
    # client.get_real_balance()
    # print(client.get_positions())
    # time.sleep(2)

    # asyncio.run(test_order())
    # print(len(client.get_markets()))
    while True:
        time.sleep(5)
        # print(client.get_all_tops())
