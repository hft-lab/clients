import asyncio
import traceback
from datetime import datetime
import json
import threading
import time
import urllib.parse
import aiohttp
from bravado.client import SwaggerClient
from bravado.requests_client import RequestsClient

# from config import Config
from clients.base_client import BaseClient
from clients.enums import ResponseStatus, OrderStatus
from tools.APIKeyAuthenticator import APIKeyAuthenticator as auth
from core.wrappers import try_exc_regular, try_exc_async


# Naive implementation of connecting to BitMEX websocket for streaming realtime data.
# The Marketmaker still interacts with this as if it were a REST Endpoint, but now it can get
# much more realtime data without polling the hell out of the API.
#
# The Websocket offers a bunch of data as raw properties right on the object.
# On connect, it synchronously asks for a push of all this data then returns.
# Right after, the MM can start using its data. It will be updated in realtime, so the MM can
# poll really often if it wants.

class BitmexClient(BaseClient):
    BASE_WS = 'wss://ws.bitmex.com/realtime'
    BASE_URL = 'https://www.bitmex.com'
    EXCHANGE_NAME = 'BITMEX'
    MAX_TABLE_LEN = 200

    def __init__(self, keys, leverage, markets_list=[], max_pos_part=20):
        super().__init__()
        self.max_pos_part = max_pos_part
        self.markets_list = markets_list
        self._loop = asyncio.new_event_loop()
        self._connected = asyncio.Event()
        self.leverage = leverage
        self.api_key = keys['API_KEY']
        self.api_secret = keys['API_SECRET']
        self.subscriptions = ['margin', 'position', 'orderBook10', 'execution']

        self.auth = auth(self.BASE_URL, self.api_key, self.api_secret)
        self.amount = 0
        self.price = 0
        self.data = {}
        self.orders = {}
        self.positions = {}
        self.orderbook = {}
        self.balance = {}
        self.keys = {}
        self.exited = False
        self.swagger_client = self.swagger_client_init()
        self.commission = self.swagger_client.User.User_getCommission().result()[0]
        self.instruments = self.get_all_instruments()
        self.markets = self.get_markets()
        self.wst = threading.Thread(target=self._run_ws_forever, daemon=True)
        self.time_sent = time.time()

    @try_exc_regular
    def get_all_instruments(self):
        # first_page = self.swagger_client.Instrument.Instrument_get(count=500).result()
        # second_page = self.swagger_client.Instrument.Instrument_get(count=500, start=500).result()
        instr_list = {}
        instruments = self.swagger_client.Instrument.Instrument_get(
            filter=json.dumps({'quoteCurrency': 'USDT', 'state': 'Open'})).result()
        for instr in instruments[0]:
            if '2' not in instr['symbol']:
                instr_list.update({instr['symbol']: instr})
        return instr_list
        # for instr in second_page[0]:
        #     if instr['state']:
        #         print(instr)
        #         print()

    @try_exc_regular
    def get_markets(self):
        markets = {}
        for market in self.instruments.values():
            if market['rootSymbol'] == 'XBT':
                markets.update({"BTC": market['symbol']})
                continue
            markets.update({market['rootSymbol']: market['symbol']})
        return markets

    @try_exc_regular
    def run_updater(self):
        self.wst.start()
        # self.__wait_for_account()
        # self.get_contract_price()

    @try_exc_regular
    def get_fees(self, symbol):
        taker_fee = self.commission[symbol]['takerFee']
        maker_fee = self.commission[symbol]['makerFee']
        return taker_fee, maker_fee

    @try_exc_regular
    def get_sizes_for_symbol(self, symbol):
        instrument = self.get_instrument(symbol)
        tick_size = instrument['tick_size']
        step_size = instrument['step_size']
        quantity_precision = len(str(step_size).split('.')[1]) if '.' in str(step_size) else 1
        return tick_size, step_size, quantity_precision

    @try_exc_regular
    def _run_ws_forever(self):
        while True:
            try:
                self._loop.run_until_complete(self._run_ws_loop())
            finally:
                print("WS loop completed. Restarting")

    @try_exc_regular
    def __wait_for_account(self):
        '''On subscribe, this data will come down. Wait for it.'''
        # Wait for the keys to show up from the ws
        while not set(self.subscriptions) <= set(self.data):
            time.sleep(0.1)

    @try_exc_async
    async def _run_ws_loop(self):
        async with aiohttp.ClientSession(headers=self.__get_auth('GET', '/realtime')) as s:
            async with s.ws_connect(self.__get_url()) as ws:
                print("Bitmex: connected")
                self._connected.set()
                try:
                    self._ws = ws
                    async for msg in ws:
                        self._process_msg(msg)
                except Exception as e:
                    traceback.print_exc()
                    print("Bitmex ws loop exited: ", e)
                finally:
                    self._connected.clear()

    @staticmethod
    @try_exc_regular
    def get_order_status(order):
        if order['ordStatus'] == 'New':
            status = OrderStatus.PROCESSING
        elif order['ordStatus'] == 'Filled':
            status = OrderStatus.FULLY_EXECUTED
        elif order['ordStatus'] == 'Canceled' and order['cumQty']:
            status = OrderStatus.PARTIALLY_EXECUTED
        elif order['ordStatus'] == 'Canceled' and not order['cumQty']:
            status = OrderStatus.NOT_EXECUTED
        else:
            status = OrderStatus.PARTIALLY_EXECUTED
        return status

    @try_exc_regular
    def get_order_result(self, order):
        factual_price = order['avgPx'] if order.get('avgPx') else 0
        factual_size_coin = abs(order['homeNotional']) if order.get('homeNotional') else 0
        factual_size_usd = abs(order['foreignNotional']) if order.get('foreignNotional') else 0
        status = self.get_order_status(order)
        result = {
            'exchange_order_id': order['orderID'],
            'exchange': self.EXCHANGE_NAME,
            'status': status,
            'factual_price': factual_price,
            'factual_amount_coin': factual_size_coin,
            'factual_amount_usd': factual_size_usd,
            'datetime_update': datetime.utcnow(),
            'ts_update': int(round(datetime.utcnow().timestamp() * 1000))
        }
        return result

    @try_exc_regular
    def update_position(self, position):
        side = 'SHORT' if position['foreignNotional'] > 0 else 'LONG'
        amount = -position['currentQty'] if side == 'SHORT' else position['currentQty']
        price = position['avgEntryPrice'] if position.get('avgEntryPrice') else 0
        self.positions.update({position['symbol']: {'side': side,
                                                    'amount_usd': -position['foreignNotional'],
                                                    'amount': amount / (10 ** 6),
                                                    'entry_price': price,
                                                    'unrealized_pnl_usd': position['unrealisedPnl'] / (10 ** 6),
                                                    'realized_pnl_usd': position['realisedPnl'] / (10 ** 6),
                                                    'lever': self.leverage}})

    @try_exc_regular
    def _process_msg(self, msg: aiohttp.WSMessage):
        if msg.type == aiohttp.WSMsgType.TEXT:
            message = json.loads(msg.data)
            if message.get('subscribe'):
                print(message)
            if message.get("action"):
                if message['table'] == 'execution':
                    for order in message['data']:
                        if order['ordStatus'] == 'New':
                            timestamp = self.timestamp_from_date(order['transactTime'])
                            print(f'BITMEX ORDER PLACE TIME: {timestamp - self.time_sent} sec')
                        result = self.get_order_result(order)
                        self.orders.update({order['orderID']: result})
                elif message['table'] == 'orderBook10':
                    for ob in message['data']:
                        if ob.get('symbol'):
                            ob.update({'timestamp': int(datetime.utcnow().timestamp() * 1000)})
                            symbol = ob['symbol']
                            self.orderbook.update({symbol: ob})
                elif message['table'] == 'position':
                    for position in message['data']:
                        if position.get('currentQty'):
                            try:
                                self.update_position(position)
                            except:
                                pass
                elif message['table'] == 'margin':
                    for balance in message['data']:
                        if balance['currency'] == 'USDt':
                            try:
                                self.balance = {'free': float(balance['availableMargin']) / (10 ** 6),
                                                'total': float(balance['marginBalance']) / (10 ** 6),
                                                'timestamp': datetime.utcnow().timestamp()}
                            except:
                                pass

    @staticmethod
    @try_exc_regular
    def timestamp_from_date(date: str):
        # date = '2023-02-15T02:55:27.640Z'
        ms = int(date.split(".")[1].split('Z')[0]) / 1000
        return time.mktime(datetime.strptime(date, "%Y-%m-%dT%H:%M:%S.%fZ").timetuple()) + ms

    @staticmethod
    @try_exc_regular
    def order_leaves_quantity(o):
        if o['leavesQty'] is None:
            return True
        return o['leavesQty'] > 0

    @staticmethod
    @try_exc_regular
    def find_by_keys(keys, table, matchData):
        for item in table:
            if all(item[k] == matchData[k] for k in keys):
                return item

    @staticmethod
    @try_exc_regular
    def get_pos_power(self, symbol):
        pos_power = 6 if 'USDT' in symbol else 8
        currency = 'USDt' if 'USDT' in symbol else 'XBt'
        return pos_power, currency

    @try_exc_regular
    def swagger_client_init(self, config=None):
        if config is None:
            # See full config options at http://bravado.readthedocs.io/en/latest/configuration.html
            config = {
                # Don't use models (Python classes) instead of dicts for #/definitions/{models}
                'use_models': False,
                # bravado has some issues with nullable fields
                'validate_responses': False,
                # Returns response in 2-tuple of (body, response); if False, will only return body
                'also_return_response': True,
            }
        spec_uri = self.BASE_URL + '/api/explorer/swagger.json'
        request_client = RequestsClient()
        request_client.authenticator = self.auth
        return SwaggerClient.from_url(spec_uri, config=config, http_client=request_client)

    @try_exc_regular
    def exit(self):
        '''Call this to exit - will close websocket.'''
        self.exited = True
        self.ws.close()

    @try_exc_regular
    def get_instrument(self, symbol):
        """Get the raw instrument data for this symbol."""
        # Turn the 'tick_size' into 'tickLog' for use in rounding
        instrument = self.instruments[symbol]
        instrument['tick_size'] = instrument['tickSize']
        instrument['step_size'] = instrument['lotSize']
        return instrument

    @try_exc_regular
    def get_balance(self):
        """Get your margin details."""
        return self.balance['total']

    @try_exc_regular
    def open_orders(self):
        """Get all your open orders."""
        return self.data['order']

    @try_exc_regular
    def fit_sizes(self, amount, price, symbol) -> None:
        tick_size, step_size, quantity_precision = self.get_sizes_for_symbol(symbol)
        self.amount = round(amount / step_size) * step_size
        if '.' in str(tick_size):
            round_price_len = len(str(tick_size).split('.')[1])
        elif '-' in str(tick_size):
            round_price_len = int(str(tick_size).split('-')[1])
        else:
            round_price_len = 0
        rounded_price = round(price / tick_size) * tick_size
        self.price = round(rounded_price, round_price_len)

    @try_exc_async
    async def create_order(self, symbol, side, session, expire=100, client_id=None):
        self.time_sent = time.time()
        body = {
            "symbol": symbol,
            "ordType": "Limit",
            "price": self.price,
            "orderQty": self.amount,
            "side": side.capitalize()
        }
        if client_id is not None:
            body["clOrdID"] = client_id

        res = await self._post("/api/v1/order", body, session)

        timestamp = 0000000000000
        exchange_order_id = None
        if res.get('errors'):
            status = ResponseStatus.ERROR
        elif res.get('order') and res['order'].get('status'):
            timestamp = int(
                datetime.timestamp(datetime.strptime(res['order']['createdAt'], '%Y-%m-%dT%H:%M:%S.%fZ')) * 1000)
            status = ResponseStatus.SUCCESS
            self.LAST_ORDER_ID = res['orderID']
            exchange_order_id = res['orderID']
        else:
            status = ResponseStatus.NO_CONNECTION

        return {
            'exchange_name': self.EXCHANGE_NAME,
            'exchange_order_id': exchange_order_id,
            'timestamp': timestamp,
            'status': status
        }

    @try_exc_async
    async def _post(self, path, data, session):
        headers_body = f"symbol={data['symbol']}&side={data['side']}&ordType=Limit&orderQty={data['orderQty']}&price={data['price']}"
        headers = self.__get_auth("POST", path, headers_body)
        headers.update(
            {
                "Content-Length": str(len(headers_body.encode('utf-8'))),
                "Content-Type": "application/x-www-form-urlencoded"}
        )
        async with session.post(url=self.BASE_URL + path, headers=headers, data=headers_body) as resp:
            return await resp.json()

    @try_exc_regular
    def change_order(self, amount, price, id):
        if amount:
            self.swagger_client.Order.Order_amend(orderID=id, orderQty=amount, price=price).result()
        else:
            self.swagger_client.Order.Order_amend(orderID=id, price=price).result()

    @try_exc_regular
    def cancel_all_orders(self):
        print('>>>>', self.swagger_client.Order.Order_cancelAll().result())
        # print('order', order)
        # if not order['ordStatus'] in ['Canceled', 'Filled']:
        #
        #     print(self.swagger_client.Order.Order_cancel(orderID=order['orderID']).result())
        #
        #     print('\n\n\n\n\n')

    @try_exc_regular
    def __get_auth(self, method, uri, body=''):
        """
        Return auth headers. Will use API Keys if present in settings.
        """
        # To auth to the WS using an API key, we generate a signature of a nonce and
        # the WS API endpoint.
        expires = str(int(round(time.time()) + 100))
        return {
            "api-expires": expires,
            "api-signature": self.auth.generate_signature(self.api_secret, method, uri, expires, body),
            "api-key": self.api_key,
        }

    @try_exc_regular
    def __get_url(self):
        """
        Generate a connection URL. We can define subscriptions right in the querystring.
        Most subscription topics are scoped by the symbol we're listening to.
        """
        # Some subscriptions need to xhave the symbol appended.
        url_parts = list(urllib.parse.urlparse(self.BASE_WS))
        url_parts[2] += "?subscribe={}".format(','.join(self.subscriptions))
        return urllib.parse.urlunparse(url_parts)

    # def get_pnl(self):
    #     positions = self.positions()
    #     pnl = [x for x in positions if x['symbol'] == self.symbol]
    #     pnl = None if not len(pnl) else pnl[0]
    #     if not pnl:
    #         return [0, 0, 0]
    #     multiplier_power = 6 if pnl['currency'] == 'USDt' else 8
    #     change = 1 if pnl['currency'] == 'USDt' else self.get_orderbook()['XBTUSD']['bids'][0][0]
    #     realized_pnl = pnl['realisedPnl'] / 10 ** multiplier_power * change
    #     unrealized_pnl = pnl['unrealisedPnl'] / 10 ** multiplier_power * change
    #     return [realized_pnl + unrealized_pnl, pnl, realized_pnl]

    @try_exc_regular
    def get_last_price(self, side, symbol):
        side = side.capitalize()
        # last_trades = self.recent_trades()
        last_trades = self.data['execution']
        last_price = 0
        for trade in last_trades:
            if trade['side'] == side and trade['symbol'] == symbol and trade.get('avgPx'):
                last_price = trade['avgPx']
        return last_price

    @try_exc_regular
    def get_real_balance(self):
        transes = None
        while not transes:
            try:
                transes = self.swagger_client.User.User_getWalletHistory(currency='USDt').result()
            except:
                pass
        real = transes[0][0]['marginBalance'] if transes[0][0]['marginBalance'] else transes[0][0]['walletBalance']
        self.balance['total'] = (real / 10 ** 6)
        self.balance['timestamp'] = datetime.utcnow().timestamp()

    @try_exc_regular
    def get_positions(self) -> dict:
        return self.positions

    @try_exc_regular
    def get_available_balance(self, side):
        # if 'USDT' in self.symbol:
        funds = [x for x in self.get_balance() if x['currency'] == 'USDt'][0]
        change = 1
        # else:
        #     funds = [x for x in self.funds() if x['currency'] == 'XBt'][0]
        #     change = self.get_orderbook()['XBTUSD']['bids'][0][0]
        positions = self.get_positions()
        wallet_balance = (funds['walletBalance'] / 10 ** self.pos_power) * change
        available_balance = wallet_balance * self.leverage
        wallet_balance = wallet_balance if self.symbol == 'XBTUSD' else 0
        position_value = 0
        for symbol, position in positions.items():
            if position['foreignNotional']:
                position_value = position['homeNotional'] * position['markPrice']

            if self.symbol == symbol and position.get('currentQty'):
                self.contract_price = abs(position_value / position['currentQty'])

        if side == 'buy':
            return available_balance - position_value - wallet_balance
        else:
            return available_balance + position_value + wallet_balance

    @try_exc_regular
    def get_position(self):
        '''Get your positions.'''
        pos_bitmex = {x['symbol']: x for x in self.swagger_client.Position.Position_get().result()[0]}
        for symbol, position in pos_bitmex.items():
            pos_bitmex[symbol] = {
                'amount': float(position['homeNotional']),
                'entry_price': float(position['avgEntryPrice']),
                'unrealized_pnl_usd': 0,
                'side': 'LONG',
                'amount_usd': -float(position['foreignNotional']),
                'realized_pnl_usd': 0,
                'lever': float(position['leverage']),
            }
        self.positions = pos_bitmex

    @try_exc_regular
    def get_orderbook(self, symbol):
        return self.orderbook[symbol]

# def get_xbt_pos(self):
#     bal_bitmex = [x for x in self.funds() if x['currency'] == 'XBt'][0]
#     xbt_pos = bal_bitmex['walletBalance'] / 10 ** 8
#     return xbt_pos

# def get_contract_price(self):
#     # self.__wait_for_account()
#     instrument = self.get_instrument()
#     self.contract_price = instrument['foreignNotional24h'] / instrument['volume24h']


if __name__ == '__main__':
    import configparser
    import sys

    config = configparser.ConfigParser()
    config.read(sys.argv[1], "utf-8")
    client = BitmexClient(keys=config['BITMEX'],
                          leverage=float(config['SETTINGS']['LEVERAGE']),
                          max_pos_part=int(config['SETTINGS']['PERCENT_PER_MARKET']),
                          markets_list=['ETH', 'BTC', 'LTC', 'BCH', 'SOL', 'MINA', 'XRP', 'PEPE', 'CFX', 'FIL'])
    client.run_updater()

    async def test_order():
        async with aiohttp.ClientSession() as session:
            ob = client.get_orderbook('RUNEUSDT')
            price = ob['bids'][5][0]
            # client.get_markets()
            client.fit_sizes(10, price, 'RUNEUSDT')
            data = await client.create_order('RUNEUSDT',
                                             'buy',
                                             session=session,
                                             client_id=f"api_deal_{str(uuid.uuid4()).replace('-', '')[:20]}")
            print(data)
            data_cancel = client.cancel_all_orders()
            print(data_cancel)

    time.sleep(3)
    print(client.markets)

    print(client.get_real_balance())
    # print(client.get_positions())
    # print(client.get_balance())
    # print(client.orders)
    while True:

        # print(client.funds())
        # print(client.get_orderbook())
        # print('CYCLE DONE')
        # print(f"{client.get_available_balance('sell')=}")
        # print(f"{client.get_available_balance('buy')=}")
        # print("\n")
        time.sleep(1)

# EXAMPLES:

# import configparser
# import sys
#
# cp = configparser.ConfigParser()
# if len(sys.argv) != 2:
#     sys.exit(1)
# cp.read(sys.argv[1], "utf-8")
# keys = cp
# keys = {'BITMEX': {'api_key': 'HKli7p7qxcsqQmsYvR-fDDmM',
#                    'api_secret': '28Dt_sLKMaGpbM2g-EI-NI1yT_mm880L56H_PhfAZ1Jug_R1',
#                    'symbol': 'XBTUSD'}}
# #
# # #
# api_key = keys["BITMEX"]["api_key"]
# api_secret = keys["BITMEX"]["api_secret"]
# bitmex_client = BitmexClient(keys['BITMEX'])
# bitmex_client.run_updater()
# #
# time.sleep(1)
#
# print(bitmex_client.get_positions())
# # loop = asyncio.new_event_loop()
# async def func():
#     async with aiohttp.ClientSession() as session:
#         await bitmex_client.create_order(0.01, 30000, 'sell', session)
# asyncio.run(func())

# time.sleep(1)
# open_orders = bitmex_client.swagger_client.Order.Order_getOrders(symbol='XBTUSD', reverse=True).result()[0]
# # print(f"{open_orders=}")
#
# for order in open_orders:
#     bitmex_client.cancel_order(order['orderID'])


# # # #
# # # /position/leverage
# # time.sleep(3)
# # funding = bitmex_client.swagger_client.Position.Position_updateLeverage(symbol='ETHUSD', leverage=10).result()
# # print(funding)
# # time.sleep(3)
# # time.sleep(1)
# # print(bitmex_client.get_positions())
# time.sleep(2)
# # print(f"BUY {bitmex_client.get_last_price('buy')}")
# while True:
#     bitmex_client.create_order(0.01, 22500, 'sell')
# time.sleep(2)
# print(f"SELL {bitmex_client.get_last_price('sell')}")

#     time.sleep(1)
#     orders = bitmex_client.open_orders()
#     for order in orders:
#         print(orders)
#         bitmex_client.cancel_order(order['orderID'])
#     print(bitmex_client.open_orders())
#     # print(f"BALANCE {bitmex_client.get_real_balance()}")
#     # print(f"POSITION {bitmex_client.get_positions()}")
#     print(f"BUY {bitmex_client.get_available_balance('buy')}")
#     print(f"SELL {bitmex_client.get_available_balance('sell')}")
#     print(f'\n\n')

# print(bitmex_client.funds())
# # bitmex_client.get_real_balance()
# while True:
#     print(bitmex_client.get_orderbook())
#     time.sleep(1)

# funding = bitmex_client.swagger_client.Funding.Funding_get(symbol='XBTUSD').result()
# print(funding)


# bal_bitmex = [x for x in self.client_Bitmex.funds() if x['currency'] == currency][0]


# while True:
#     pos_bitmex = [x for x in bitmex_client.positions if x['symbol'] == 'XBTUSDT'][0]
#     side = 'Buy' if pos_bitmex['currentQty'] < 0 else 'Sell'
#     size = abs(pos_bitmex['currentQty'])
#     open_orders = bitmex_client.open_orders(clOrdIDPrefix='')
#     price = orderbook['asks'][0][0] if side == 'Sell' else orderbook['bids'][0][0]
#     exist = False
#     for order in open_orders:
#         if 'CANCEL' in order['clOrdID']:
#             if price != order['price']:
#                 bitmex_client.change_order(size, price, order['orderID'])
#                 print(f"Changed {size}")
#             exist = True
#             break
#     if exist:
#         continue
#     bitmex_client.create_order(size, price, side, 'Limit', 'CANCEL')

# print(bitmex_client.get_available_balance('Sell'))
# print(bitmex_client.get_available_balance('Buy'))
# # while True:
#     # orders = bitmex_client.swagger_client.Order.Order_getOrders(symbol='XBTUSD', reverse=True).result()[0]
# a = bitmex_client.get_instrument()
# print(a)
# print(f"volume24h: {a['volume24h']}")
# print(f"homeNotional24h: {a['homeNotional24h']}")
# print(f"turnover24h: {a['turnover24h']}")
# print(f"foreignNotional24h: {a['foreignNotional24h']}")
# contract_price = a['foreignNotional24h'] / a['volume24h']
# print(contract_price)
# orders = bitmex_client.swagger_client.User.User_getExecutionHistory(symbol='XBTUSD', timestamp=date).result()
# if len(orders[0]):
#     print(orders[0])
#     break
# for order in orders[0]:
#     print(f"Time: {order['transactTime']}")
#     print(f"Order ID: {order['clOrdID']}")
#     # print(f"Realized PNL: {order['realisedPnl'] / 100000000} USD")
#     print(f"Side: {order['side']}")
#     print(f"Order size: {order['orderQty']} USD")
#     print(f"Price: {order['price']}")
#     print()
# orders = bitmex_client.swagger_client.Settlement.Settlement_get(symbol='XBTUSDT',).result()
# orders = bitmex_client.swagger_client.User.User_getWalletHistory(currency='USDt',).result()
# instruments = bitmex_client.swagger_client.Instrument.Instrument_getActiveAndIndices().result()
# print(instruments)
# for instrument in instruments[0]:
#     print(instrument['symbol'])
# time.sleep(1)
# orderbook = bitmex_client.get_orderbook()()['XBT/USDT']
# print(orderbook)
# print(orders)
# money = bitmex_client.funds()
# print(money)
#     bitmex_client.create_order(size, price, side, 'Limit', 'CANCEL')
# bitmex_client.create_order(1000, 12000, 'Buy', 'Limit', 'CANCEL1')
# time.sleep(1)

#     print(order)


# orders = bitmex_client.open_orders('')
# print(orders)

# TRANZACTION HISTORY
# orders = bitmex_client.swagger_client.User.User_getWalletHistory(currency='USDt',).result()
#
# for tranz in orders[0]:
#     print("TRANZ:" + tranz['transactID'])
#     print("type:" + str(tranz['transactType']))
#     print("status:" + str(tranz['transactStatus']))
#     print("amount:" + str(tranz['amount'] / (10 ** 6)))
#     if tranz['fee']:
#         print("fee:" + str(tranz['fee'] / (10 ** 6)))
#     print("walletBalance:" + str(tranz['walletBalance'] / (10 ** 6)))
#     if tranz['marginBalance']:
#         print("marginBalance:" + str(tranz['marginBalance'] / (10 ** 6)))
#     print('Timestamp:' + str(tranz['timestamp']))
#     print()
# time.sleep(1)


# time.sleep(1)
# open_orders = bitmex_client.open_orders(clOrdIDPrefix='BALANCING')
# print(open_orders)

# open_orders_resp = [{'orderID': '20772f48-24a3-4cff-a470-670d51a1666e', 'clOrdID': 'BALANCING', 'clOrdLinkID': '', 'account': 2133275,
#   'symbol': 'XBTUSDT', 'side': 'Buy', 'simpleOrderQty': None, 'orderQty': 1000, 'price': 15000, 'displayQty': None,
#   'stopPx': None, 'pegOffsetValue': None, 'pegPriceType': '', 'currency': 'USDT', 'settlCurrency': 'USDt',
#   'ordType': 'Limit', 'timeInForce': 'GoodTillCancel', 'execInst': '', 'contingencyType': '', 'exDestination': 'XBME',
#   'ordStatus': 'New', 'triggered': '', 'workingIndicator': True, 'ordRejReason': '', 'simpleLeavesQty': None,
#   'leavesQty': 1000, 'simpleCumQty': None, 'cumQty': 0, 'avgPx': None, 'multiLegReportingType': 'SingleSecurity',
#   'text': 'Submitted via API.', 'transactTime': '2022-11-16T17:18:45.740Z', 'timestamp': '2022-11-16T17:18:45.740Z'}]
# bitmex_client.create_order(1000, 17000, 'Buy', 'Limit', 'BALANCING BTC2')
# time.sleep(1)
# print(commission.result())
# print(orders.objRef)
# print(orders.op)
# print(orders.status)
# bitmex_client.cancel_order(id)
# print(bitmex_client.data['execution'])
# print(bitmex_client.recent_trades())
# print(bitmex_client.funds()[1])
# print(bitmex_client.get_orderbook()())

#   [{'orderID': 'baf6fc1e-8f76-4090-a3f3-254314da86b4', 'clOrdID': 'BALANCING BTC', 'clOrdLinkID': '', 'account': 2133275,
#   'symbol': 'XBTUSDT', 'side': 'Buy', 'simpleOrderQty': None, 'orderQty': 1000, 'price': 17000, 'displayQty': None,
#   'stopPx': None, 'pegOffsetValue': None, 'pegPriceType': '', 'currency': 'USDT', 'settlCurrency': 'USDt',
#   'ordType': 'Limit', 'timeInForce': 'GoodTillCancel', 'execInst': '', 'contingencyType': '', 'exDestination': 'XBME',
#   'ordStatus': 'New', 'triggered': '', 'workingIndicator': True, 'ordRejReason': '', 'simpleLeavesQty': None,
#   'leavesQty': 1000, 'simpleCumQty': None, 'cumQty': 0, 'avgPx': None, 'multiLegReportingType': 'SingleSecurity',
#   'text': 'Submitted via API.', 'transactTime': '2022-11-16T17:24:10.721Z', 'timestamp': '2022-11-16T17:24:10.721Z',
#   'lastQty': None, 'lastPx': None, 'lastLiquidityInd': '', 'tradePublishIndicator': '',
#   'trdMatchID': '00000000-0000-0000-0000-000000000000', 'execID': 'cee84b5e-3946-afe5-c1c7-f7d99945dfd7',
#   'execType': 'New', 'execCost': None, 'homeNotional': None, 'foreignNotional': None, 'commission': None,
#   'lastMkt': '', 'execComm': None, 'underlyingLastPx': None},
#    {'orderID': 'baf6fc1e-8f76-4090-a3f3-254314da86b4',
#   'clOrdID': 'BALANCING BTC', 'clOrdLinkID': '', 'account': 2133275, 'symbol': 'XBTUSDT', 'side': 'Buy',
#   'simpleOrderQty': None, 'orderQty': 1000, 'price': 17000, 'displayQty': None, 'stopPx': None, 'pegOffsetValue': None,
#   'pegPriceType': '', 'currency': 'USDT', 'settlCurrency': 'USDt', 'ordType': 'Limit', 'timeInForce': 'GoodTillCancel',
#   'execInst': '', 'contingencyType': '', 'exDestination': 'XBME', 'ordStatus': 'Filled', 'triggered': '',
#   'workingIndicator': False, 'ordRejReason': '', 'simpleLeavesQty': None, 'leavesQty': 0, 'simpleCumQty': None,
#   'cumQty': 1000, 'avgPx': 16519, 'multiLegReportingType': 'SingleSecurity', 'text': 'Submitted via API.',
#   'transactTime': '2022-11-16T17:24:10.721Z', 'timestamp': '2022-11-16T17:24:10.721Z', 'lastQty': 1000, 'lastPx': 16519,
#   'lastLiquidityInd': 'RemovedLiquidity', 'tradePublishIndicator': 'PublishTrade',
#   'trdMatchID': '15cd273d-ded8-e339-b3b1-9a9080b5d10f', 'execID': 'adcc6b75-2d57-a9d2-47c4-8db921d8aae1',
#   'execType': 'Trade', 'execCost': 16519000, 'homeNotional': 0.001, 'foreignNotional': -16.519,
#   'commission': 0.00022500045, 'lastMkt': 'XBME', 'execComm': 3716, 'underlyingLastPx': None}]
