import time
import aiohttp
import json
import requests
from datetime import datetime
import asyncio
import threading
import zlib


# from core.wrappers import try_exc_regular, try_exc_async


class CoinTigerClient:
    PUBLIC_WS_ENDPOINT = 'wss://api.cointiger.com/exchange-market/ws'
    EXCHANGE_NAME = 'COINTIGER'
    MARKET_ENDPOINT = 'https://api.cointiger.com/exchange/trading/api/market'
    TRADE_ENDPOINT = 'https://api.cointiger.com/exchange/trading/api/v2'

    def __init__(self, keys=None, leverage=None, markets_list=[], max_pos_part=20):
        self.headers = {'Content-Type': 'application/json'}
        self.markets = self.get_markets()
        self._loop_public = asyncio.new_event_loop()
        self._connected = asyncio.Event()
        self.wst_public = threading.Thread(target=self._run_ws_forever, args=[self._loop_public])
        self.requestLimit = 1200
        self.getting_ob = asyncio.Event()
        self.now_getting = ''
        self.orderbook = {}
        self.taker_fee = 0.0006

    # @try_exc_regular
    def get_markets(self):
        way = f"{self.TRADE_ENDPOINT}/currencys/v2"
        resp = requests.get(url=way, headers=self.headers).json()
        markets = {}
        for market in resp['data']['usdt-partition']:
            markets.update({market['baseCurrency'].upper(): (market['baseCurrency'] + market['quoteCurrency']).upper()})
        return markets
        # example = {'code': '0', 'msg': 'suc', 'data': {'usdt-partition': [
        #     {'baseCurrency': 'tep', 'quoteCurrency': 'usdt', 'pricePrecision': 4, 'amountPrecision': 4,
        #      'amountMin': 0.0001, 'withdrawFeeMin': 2500.0, 'withdrawFeeMax': 2501.0, 'withdrawOneMin': 3750.0,
        #      'withdrawOneMax': 17000000.0, 'depthSelect': {'step0': '0.0001', 'step1': '0.01', 'step2': '0.1'},
        #      'minTurnover': 10.0},
        #     {'baseCurrency': 'xoc', 'quoteCurrency': 'usdt', 'pricePrecision': 4, 'amountPrecision': 2,
        #      'amountMin': 0.01, 'withdrawFeeMin': 5.0, 'withdrawFeeMax': 6.0, 'withdrawOneMin': 7.5,
        #      'withdrawOneMax': 32400.0, 'depthSelect': {'step0': '0.0001', 'step1': '0.01', 'step2': '0.1'},
        #      'minTurnover': 10.0},
        #     {'baseCurrency': 'xpe', 'quoteCurrency': 'usdt', 'pricePrecision': 6, 'amountPrecision': 2,
        #      'amountMin': 0.01, 'withdrawFeeMin': 60.0, 'withdrawFeeMax': 61.0, 'withdrawOneMin': 90.0,
        #      'withdrawOneMax': 1100000.0, 'depthSelect': {'step0': '0.000001', 'step1': '0.00001', 'step2': '0.001'},
        #      'minTurnover': 10.0}}}

    #     @try_exc_regular
    def get_all_tops(self):
        tops = {}
        for coin, symbol in self.markets.items():
            orderbook = self.get_orderbook(symbol)
            if len(orderbook['bids']) and len(orderbook['asks']):
                tops.update({self.EXCHANGE_NAME + '__' + coin: {
                    'top_bid': orderbook['bids'][0][0], 'top_ask': orderbook['asks'][0][0],
                    'bid_vol': orderbook['bids'][0][1], 'ask_vol': orderbook['asks'][0][1],
                    'ts_exchange': orderbook['timestamp']}})
        return tops

    @staticmethod
    def get_all_markets(self):
        url = 'https://www.cointiger.com/exchange/api/public/market/detail'
        resp = requests.get(url=url)
        print(resp.json())

    #     @try_exc_regular
    def _run_ws_forever(self, loop):
        while True:
            loop.run_until_complete(self._run_ws_loop())
            time.sleep(1)

    #     @try_exc_async
    async def _run_ws_loop(self):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(self.PUBLIC_WS_ENDPOINT) as ws:
                print(f"CONNECTED")
                self._connected.set()
                self._ws_public = ws
                for symbol in self.markets.values():
                    await self._loop_public.create_task(self.subscribe_orderbooks(symbol))
                async for msg in ws:
                    # print(msg)
                    # print(json.loads(msg))
                    data = json.loads(self.decode_gzip_data(msg.data))
                    if ping := data.get('ping'):
                        await ws.pong(bytes(ping))
                    print(data)
                    # self.update_orderbook(data)

    @staticmethod
    #     @try_exc_regular
    def decode_gzip_data(data):
        # Decompress the Gzip data
        decompressed_data = zlib.decompress(data, 16 + zlib.MAX_WBITS)

        # Convert decompressed data from binary to a UTF-8 encoded string
        string_data = decompressed_data.decode('utf-8')
        return string_data

    #     @try_exc_async
    async def subscribe_orderbooks(self, symbol):
        method = {"event": "sub",
                  "params": {
                      "channel": f"market_{symbol.lower()}_depth_step_0",
                      "cb_id": "abrakadabra",
                      "asks": 10,
                      "bids": 10}}
        await self._connected.wait()
        await self._ws_public.send_json(method)

    #     @try_exc_regular
    def update_orderbook(self, data):
        ob = data['d'][0]
        symbol = ob['s']
        for new_bid in ob['b']:
            res = self.orderbook[symbol]['bids']
            if res.get(new_bid[0]) and new_bid[1] == '0':
                del res[new_bid[0]]
            else:
                self.orderbook[symbol]['bids'][new_bid[0]] = new_bid[1]
        for new_ask in ob['a']:
            res = self.orderbook[symbol]['asks']
            if res.get(new_ask[0]) and new_ask[1] == '0':
                del res[new_ask[0]]
            else:
                self.orderbook[symbol]['asks'][new_ask[0]] = new_ask[1]
        self.orderbook[symbol]['timestamp'] = ob['t']

    #     @try_exc_regular
    def update_orderbook_snapshot(self, data):
        ob = data['d'][0]
        symbol = ob['s']
        self.orderbook[symbol] = {'asks': {x[0]: x[1] for x in ob['a']},
                                  'bids': {x[0]: x[1] for x in ob['b']},
                                  'timestamp': ob['t']}

    #     @try_exc_regular
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

    async def get_async_ob(self, symbol, session):
        path = f'/depth?api_key=100310001&symbol={symbol}&type=step0'
        async with session.get(self.MARKET_ENDPOINT + path) as response:
            print(await response.json())


if __name__ == '__main__':
    client = CoinTigerClient()
    print(client.get_markets())
    client.run_updater()

    async def testing():
        async with aiohttp.ClientSession() as session:
            loop = asyncio.get_event_loop()
            tasks = []
            for market in client.markets.values():
                tasks.append(loop.create_task(client.get_async_ob(market, session)))
            await asyncio.gather(*tasks, return_exceptions=True)


    while True:
        time.sleep(0.1)
        # client.get_all_markets()
        # time_start = time.time()
        # loop = asyncio.new_event_loop()
        # loop.run_until_complete(testing())
        # print(f"TIME: {time.time() - time_start} sec")
    #     print(client.get_all_tops())