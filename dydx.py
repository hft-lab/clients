import asyncio
import traceback
import uuid
from datetime import datetime
import json
import threading
import time
from pprint import pprint

import aiohttp
import requests
from dydx3 import Client
from dydx3.constants import API_HOST_MAINNET
from dydx3.constants import NETWORK_ID_MAINNET
from dydx3.helpers.request_helpers import epoch_seconds_to_iso
from dydx3.helpers.request_helpers import generate_now_iso
from dydx3.helpers.request_helpers import random_client_id
from dydx3.helpers.request_helpers import remove_nones
from dydx3.starkex.order import SignableOrder
from web3 import Web3

# from config import Config
from clients.base_client import BaseClient
from clients.enums import ResponseStatus, OrderStatus, ClientsOrderStatuses
from core.wrappers import try_exc_regular, try_exc_async


class DydxClient(BaseClient):
    BASE_WS = 'wss://api.dydx.exchange/v3/ws'
    BASE_URL = 'https://api.dydx.exchange'
    EXCHANGE_NAME = 'DYDX'
    urlMarkets = "https://api.dydx.exchange/v3/markets/"
    urlOrderbooks = "https://api.dydx.exchange/v3/orderbook/"

    def __init__(self, keys, leverage, markets_list=[], max_pos_part=20):
        super().__init__()
        self.markets_list = markets_list
        self.max_pos_part = max_pos_part
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._connected = asyncio.Event()
        self.headers = {"Content-Type": "application/json"}
        self.API_KEYS = {"secret": keys['API_SECRET'],
                         "key": keys['API_KEY'],
                         "passphrase": keys['PASSPHRASE']}
        self.client = Client(
            network_id=NETWORK_ID_MAINNET,
            host=API_HOST_MAINNET,
            default_ethereum_address=keys['ETH_ADDRESS'],
            web3=Web3(Web3.WebsocketProvider(f'wss://mainnet.infura.io/ws/v3/{keys["INFURA_KEY"]}')),
            eth_private_key=keys['ETH_PRIVATE_KEY'],
            stark_private_key=keys['PRIVATE_KEY'],
            stark_public_key=keys['PUBLIC_KEY'],
            stark_public_key_y_coordinate=keys['PUBLIC_KEY_Y_COORDINATE'],
            web3_provider=f'https://mainnet.infura.io/v3/{keys["INFURA_KEY"]}',
            api_key_credentials=self.API_KEYS
        )
        self.error_info = None
        self.orders = {}
        self.fills = {}
        self.positions = {}
        self.count_flag = False

        self.requestLimit = 1050  # 175 за 10 секунд https://dydxprotocol.github.io/v3-teacher/#rate-limit-api
        self.markets = {}
        self.balance = {'free': 0, 'total': 0, 'timestamp': 0}
        self.orderbook = {}
        self.amount = 0
        self.price = 0

        self.keys = keys
        self.user = self.client.private.get_user().data
        self.account = self.client.private.get_account().data
        self.instruments = self.client.public.get_markets().data
        self.leverage = leverage

        self.get_real_balance()

        # self.maker_fee = float(self.user['user']['makerFeeRate'])
        self.taker_fee = float(self.user['user']['takerFeeRate'])

        self._updates = 0
        self.offsets = {}
        # self.time_sent = time.time()
        self.get_position()
        self.get_markets()
        self.wst = threading.Thread(target=self._run_ws_forever, daemon=True)
        self.pings = []

    @try_exc_regular
    def get_position(self):
        # NECESSARY
        self.positions = {}
        for pos in self.client.private.get_positions().data.get('positions', []):
            if pos['status'] != 'CLOSED':
                self.positions.update({pos['market']: {
                    'side': pos['side'],
                    'amount_usd': float(pos['size']) * float(pos['entryPrice']),
                    'amount': float(pos['size']),
                    'entry_price': float(pos['entryPrice']),
                    'unrealized_pnl_usd': float(pos['unrealizedPnl']),
                    'realized_pnl_usd': float(pos['realizedPnl']),
                    'lever': self.leverage
                }})

    @try_exc_regular
    def cancel_all_orders(self, order_id=None):
        # NECESSARY
        for coin in self.markets_list:
            market = self.markets[coin]
            self.client.private.cancel_active_orders(market=market)

    @try_exc_regular
    def get_real_balance(self):
        # NECESSARY
        try:
            res = self.client.private.get_account().data
            self.balance = {'free': float(res['account']['freeCollateral']),
                            'total': float(res['account']['equity']),
                            'timestamp': time.time()}
            self.position_id = self.account['account']['positionId']
        except:
            pass

    @try_exc_regular
    def exit(self):
        self._ws.close()
        while True:
            try:
                self._loop.stop()
                self._loop.close()
                return
            except:
                pass

    # async def test_create_order(self, amount: str, price: str, side: str, type: str) -> dict:
    #     async with aiohttp.ClientSession() as session:
    #        await self.create_order(amount, price, side, session, type)

    @try_exc_regular
    def get_markets(self):
        # NECESSARY
        # markets = requests.get(url=self.urlMarkets, headers=self.headers).json()
        for market, value in self.instruments['markets'].items():
            if value['quoteAsset'] == 'USD' and value['status'] == 'ONLINE':
                coin = value['baseAsset']
                self.markets.update({coin: market})
        return self.markets

    @try_exc_async
    async def get_multi_orderbook(self, symbol):
        async with aiohttp.ClientSession() as session:
            async with session.get(self.urlOrderbooks + symbol) as response:
                ob = await response.json()
                try:
                    return {'top_bid': ob['bids'][0]['price'], 'top_ask': ob['asks'][0]['price'],
                            'bid_vol': ob['bids'][0]['size'], 'ask_vol': ob['asks'][0]['size'],
                            'ts_exchange': 0}
                except Exception as error:
                    print(f"Error from DyDx Module, symbol: {symbol}, error: {error}")

    @try_exc_regular
    def get_all_tops(self):
        # NECESSARY
        tops = {}
        for symbol, orderbook in self.orderbook.items():
            coin = symbol.upper().split('-')[0]
            if len(orderbook['bids']) and len(orderbook['asks']):
                tops.update({self.EXCHANGE_NAME + '__' + coin:
                                 {'top_bid': orderbook['bids'][0][0], 'top_ask': orderbook['asks'][0][0],
                                  'bid_vol': orderbook['bids'][0][1], 'ask_vol': orderbook['asks'][0][1],
                                  'ts_exchange': orderbook['timestamp']}})

        return tops

    @try_exc_async
    async def get_funding_payments(self, session: aiohttp.ClientSession):
        data = {}
        now_iso_string = generate_now_iso()
        request_path = f'/v3/funding?limit=100'
        signature = self.client.private.sign(
            request_path=request_path,
            method='GET',
            iso_timestamp=now_iso_string,
            data=remove_nones(data)
        )

        headers = {
            'DYDX-SIGNATURE': signature,
            'DYDX-API-KEY': self.API_KEYS['key'],
            'DYDX-TIMESTAMP': now_iso_string,
            'DYDX-PASSPHRASE': self.API_KEYS['passphrase']
        }

        async with session.get(url=self.BASE_URL + request_path, headers=headers,
                               data=json.dumps(remove_nones(data))) as resp:
            res = await resp.json()
            fundings = res['fundingPayments']
            for fund in fundings:
                datetime_obj = datetime.fromisoformat(fund['effectiveAt'][:-1])
                timestamp_ms = int(datetime_obj.timestamp() * 1000)
                fund.update({'time': timestamp_ms,
                             'datetime': datetime.strptime(fund['effectiveAt'], '%Y-%m-%dT%H:%M:%S.%fZ'),
                             'asset': 'USDC',
                             'tranId': 'hasNoTranId'})
            return fundings

    @try_exc_async
    async def get_order_by_id(self, symbol, order_id: str, session: aiohttp.ClientSession):
        data = {}
        now_iso_string = generate_now_iso()
        request_path = f'/v3/orders/{order_id}'
        signature = self.client.private.sign(
            request_path=request_path,
            method='GET',
            iso_timestamp=now_iso_string,
            data=remove_nones(data),
        )

        headers = {
            'DYDX-SIGNATURE': signature,
            'DYDX-API-KEY': self.API_KEYS['key'],
            'DYDX-TIMESTAMP': now_iso_string,
            'DYDX-PASSPHRASE': self.API_KEYS['passphrase']
        }

        async with session.get(url=self.BASE_URL + request_path, headers=headers,
                               data=json.dumps(remove_nones(data))) as resp:
            res = await resp.json()
            if res := res.get('order'):
                return {
                    'exchange_order_id': order_id,
                    'exchange': self.EXCHANGE_NAME,
                    'status': OrderStatus.FULLY_EXECUTED if res.get(
                        'status') == ClientsOrderStatuses.FILLED else OrderStatus.NOT_EXECUTED,
                    'factual_price': float(res['price']),
                    'factual_amount_coin': float(res['size']),
                    'factual_amount_usd': float(res['size']) * float(res['price']),
                    'datetime_update': datetime.utcnow(),
                    'ts_update': int(datetime.utcnow().timestamp() * 1000)
                }
            # else:

                # print(f"DYDX get_order_by_id {res=}")

    @try_exc_async
    async def get_all_orders(self, symbol, session) -> list:
        # NECESSARY
        data = {}
        now_iso_string = generate_now_iso()
        request_path = f'/v3/orders?market={symbol}'
        signature = self.client.private.sign(
            request_path=request_path,
            method='GET',
            iso_timestamp=now_iso_string,
            data=remove_nones(data),
        )

        headers = {
            'DYDX-SIGNATURE': signature,
            'DYDX-API-KEY': self.API_KEYS['key'],
            'DYDX-TIMESTAMP': now_iso_string,
            'DYDX-PASSPHRASE': self.API_KEYS['passphrase']
        }
        orders = []
        async with session.get(url=self.BASE_URL + request_path, headers=headers,
                               data=json.dumps(remove_nones(data))) as resp:
            res = await resp.json()
            # print(f"{self.EXCHANGE_NAME} get_all_orders {res=}")
            try:
                for order in res['orders']:
                    if res.get('status') == ClientsOrderStatuses.FILLED and float(res['origQty']) > float(
                            res['executedQty']):
                        status = OrderStatus.PARTIALLY_EXECUTED
                    elif res.get('status') == ClientsOrderStatuses.FILLED:
                        status = OrderStatus.FULLY_EXECUTED
                    else:
                        status = OrderStatus.NOT_EXECUTED

                    orders.append(
                        {
                            'id': uuid.uuid4(),
                            'datetime': datetime.strptime(order['createdAt'], '%Y-%m-%dT%H:%M:%SZ'),
                            'ts': int(time.time()),
                            'context': 'web-interface' if not 'api_' in order['clientId'] else
                            order['clientId'].split('_')[1],
                            'parent_id': uuid.uuid4(),
                            'exchange_order_id': order['id'],
                            'type': order['timeInForce'],
                            'status': status,
                            'exchange': self.EXCHANGE_NAME,
                            'side': order['side'].lower(),
                            'symbol': symbol,
                            'expect_price': float(order['price']),
                            'expect_amount_coin': float(order['size']),
                            'expect_amount_usd': float(order['price']) * float(order['size']),
                            'expect_fee': self.taker_fee,
                            'factual_price': float(order['price']),
                            'factual_amount_coin': float(order['size']),
                            'factual_amount_usd': float(order['size']) * float(order['price']),
                            'factual_fee': self.taker_fee,
                            'order_place_time': 0,
                            'env': '-',
                            'datetime_update': datetime.utcnow(),
                            'ts_update': int(time.time()),
                            'client_id': order['clientId']
                        }
                    )
            except:
                traceback.print_exc()

            return orders

    @try_exc_regular
    def get_sizes_for_symbol(self, symbol):
        tick_size = float(self.instruments['markets'][symbol]['tickSize'])
        step_size = float(self.instruments['markets'][symbol]['stepSize'])
        quantity_precision = len(str(step_size).split('.')[1]) if '.' in str(step_size) else 1
        return tick_size, step_size, quantity_precision

    @try_exc_regular
    def fit_sizes(self, amount, price, symbol):
        # NECESSARY
        tick_size, step_size, quantity_precision = self.get_sizes_for_symbol(symbol)
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
        return self.price, self.amount

    @try_exc_async
    async def create_order(self, symbol, side, session, expire=10000, client_id=None, expiration=None) -> dict:
        # NECESSARY
        time_sent = datetime.utcnow().timestamp()
        expire_date = int(round(time.time()) + expire)
        now_iso_string = generate_now_iso()
        expiration = expiration or epoch_seconds_to_iso(expire_date)
        client_id = client_id if client_id else random_client_id()
        order_to_sign = SignableOrder(
            network_id=NETWORK_ID_MAINNET,
            position_id=self.position_id,
            client_id=client_id,
            market=symbol,
            side=side.upper(),
            human_size=str(self.amount),
            human_price=str(self.price),
            limit_fee='0.0008',
            expiration_epoch_seconds=expire_date,
        )
        try:
            data = {
                'market': symbol,
                'side': side.upper(),
                'type': type.upper(),
                'timeInForce': 'GTT',
                'size': str(self.amount),
                'price': str(self.price),
                'limitFee': '0.0008',
                'expiration': expiration,
                'postOnly': False,
                'clientId': client_id,
                'signature': order_to_sign.sign(self.keys['PRIVATE_KEY']),
                'cancelId': None,
                'triggerPrice': None,
                'trailingPercent': None,
                'reduceOnly': None
            }
        except Exception as e:
            self.error_info = str(e)
            return {
                'exchange_name': self.EXCHANGE_NAME,
                'timestamp': 0,
                'status': ResponseStatus.ERROR
            }
        print(f'DYDX BODY: {data}')
        request_path = '/v3/orders'
        signature = self.client.private.sign(
            request_path=request_path,
            method='POST',
            iso_timestamp=now_iso_string,
            data=remove_nones(data),
        )

        headers = {
            'DYDX-SIGNATURE': signature,
            'DYDX-API-KEY': self.API_KEYS['key'],
            'DYDX-TIMESTAMP': now_iso_string,
            'DYDX-PASSPHRASE': self.API_KEYS['passphrase'],

            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'User-Agent': 'dydx/python'
        }

        try:
            async with session.post(url=self.BASE_URL + request_path, headers=headers,
                                    data=json.dumps(remove_nones(data))) as resp:
                res = await resp.json()
                self.LAST_ORDER_ID = res.get('order', {'id': 'default'})['id']
                exchange_order_id = res.get('order', {'id': 'default'})['id']
                timestamp = 0000000000000
                if res.get('errors'):
                    status = ResponseStatus.ERROR
                    self.error_info = res
                elif res.get('order') and res['order'].get('status'):
                    timestamp = int(
                        datetime.timestamp(datetime.strptime(res['order']['createdAt'], '%Y-%m-%dT%H:%M:%S.%fZ')) * 1000)
                    status = ResponseStatus.SUCCESS
                else:
                    status = ResponseStatus.NO_CONNECTION
                ping = int(round(timestamp - (time_sent * 1000), 0))
                self.pings.append(ping)
                with open(f'{self.EXCHANGE_NAME}_pings.txt', 'a') as file:
                    file.write(str(datetime.utcnow()) + ' ' + str(ping) + '\n')
                avr = int(round((sum(self.pings) / len(self.pings)), 0))
                print(f"{self.EXCHANGE_NAME}: ping {ping}|avr: {avr}|max: {max(self.pings)}|min: {min(self.pings)}")
                return {
                    'exchange_name': self.EXCHANGE_NAME,
                    'exchange_order_id': exchange_order_id,
                    'timestamp': timestamp,
                    'status': status
                }
        except Exception as e:
            self.error_info = e
            return {
                'exchange_name': self.EXCHANGE_NAME,
                'exchange_order_id': None,
                'timestamp': int(round(datetime.utcnow().timestamp() * 1000)),
                'status': ResponseStatus.ERROR
            }

    # def get_funding_history(self):
    #     return self.client.public.get_historical_funding(market='ETH').data

    # def get_funding_payments(self):
    #     return self.client.private.get_funding_payments(market=self.symbol, limit=300).data

    @try_exc_regular
    def run_updater(self):
        # NECESSARY
        self.wst.start()
        # except Exception as e:
        #     print(f"Error line 33: {e}")

    @try_exc_regular
    def _run_ws_forever(self):
        while True:
            try:
                self._loop.run_until_complete(self._run_ws_loop())
            finally:
                print("WS loop completed. Restarting")

    @try_exc_async
    async def _run_ws_loop(self):
        async with aiohttp.ClientSession() as s:
            try:
                async with s.ws_connect(self.BASE_WS) as ws:
                    print("DyDx: connected")
                    self._connected.set()
                    self._ws = ws
                    self._loop.create_task(self._subscribe_orderbooks())
                    self._loop.create_task(self._subscribe_account())
                    async for msg in ws:
                        self._process_msg(msg)
            except Exception as e:
                print("DyDx ws loop exited: ")
                traceback.print_exc()
            finally:
                self._connected.clear()

    @try_exc_async
    async def _subscribe_account(self):
        now_iso_string = generate_now_iso()
        signature = self.client.private.sign(
            request_path='/ws/accounts',
            method='GET',
            iso_timestamp=now_iso_string,
            data={},
        )
        msg = {
            'type': 'subscribe',
            'channel': 'v3_accounts',
            'accountNumber': '0',
            'apiKey': self.keys['API_KEY'],
            'passphrase': self.keys['PASSPHRASE'],
            'timestamp': now_iso_string,
            'signature': signature,
        }
        await self._connected.wait()
        await self._ws.send_json(msg)

    @try_exc_async
    async def _subscribe_orderbooks(self):
        # self.markets_list = list(self.markets.keys())[:10]
        for symbol in self.markets_list:
            if market := self.markets.get(symbol):
                msg = {
                    'type': 'subscribe',
                    'channel': 'v3_orderbook',
                    'id': market,
                    'includeOffsets': True
                }
                await self._connected.wait()
                await self._ws.send_json(msg)

    @try_exc_regular
    def _first_orderbook_update(self, ob: dict):
        symbol = ob['id']
        self.orderbook.update({symbol: {'asks': [], 'bids': [], 'timestamp': None}})
        ob = ob['contents']
        for ask in ob['asks']:
            if float(ask['size']) > 0:
                self.orderbook[symbol]['asks'].append([float(ask['price']), float(ask['size']), int(ask['offset'])])
            self.offsets[ask['price']] = int(ask['offset'])
        for bid in ob['bids']:
            if float(bid['size']) > 0:
                self.orderbook[symbol]['bids'].append([float(bid['price']), float(bid['size']), int(bid['offset'])])
            self.offsets[bid['price']] = int(bid['offset'])
        self.orderbook[symbol]['asks'] = sorted(self.orderbook[symbol]['asks'])
        self.orderbook[symbol]['bids'] = sorted(self.orderbook[symbol]['bids'])[::-1]
        self.orderbook[symbol].update({'timestamp': int(time.time() * 1000)})
        self.count_flag = True

    @try_exc_regular
    def _append_new_order(self, ob, side):
        symbol = ob['id']
        ob = ob['contents']
        offset = int(ob['offset'])
        for new_order in ob[side]:
            if self.offsets.get(new_order[0]):
                if self.offsets[new_order[0]] > offset:
                    continue
            self.offsets[new_order[0]] = offset
            new_order = [float(new_order[0]), float(new_order[1]), offset]
            index = 0
            for order in self.orderbook[symbol][side]:
                if new_order[0] == order[0]:
                    if new_order[1] != 0.0:
                        order[1] = new_order[1]
                        order[2] = offset
                        break
                    else:
                        self.orderbook[symbol][side].remove(order)
                        break
                if side == 'bids':
                    if new_order[0] > order[0]:
                        self.orderbook[symbol][side].insert(index, new_order)
                        break
                elif side == 'asks':
                    if new_order[0] < order[0]:
                        self.orderbook[symbol][side].insert(index, new_order)
                        break
                index += 1
            if index == 0:
                self._check_for_error(symbol)
        self.orderbook[symbol]['timestamp'] = int(time.time())

    @try_exc_regular
    def _channel_orderbook_update(self, ob: dict):
        symbol = ob['id']
        last_ob_ask = self.orderbook[symbol]['asks'][0][0]
        last_ob_bid = self.orderbook[symbol]['bids'][0][0]
        if len(ob['contents']['bids']):
            self._append_new_order(ob, 'bids')
        if len(ob['contents']['asks']):
            self._append_new_order(ob, 'asks')
        self.orderbook[symbol]['timestamp'] = int(datetime.utcnow().timestamp() * 1000)
        if last_ob_ask != self.orderbook[symbol]['asks'][0][0] or last_ob_bid != self.orderbook[symbol]['bids'][0][0]:
            self.count_flag = True
        # print(f"\n\nDYDX NEW OB APPEND TIME: {time.time() - time_start} sec\n\n")

    @try_exc_regular
    def _check_for_error(self, symbol):
        orderbook = self.orderbook[symbol]
        top_ask = orderbook['asks'][0]
        top_bid = orderbook['bids'][0]
        if top_ask[0] < top_bid[0]:
            if top_ask[2] <= top_bid[2]:
                self.orderbook[symbol]['asks'].remove(top_ask)
            else:
                self.orderbook[symbol]['bids'].remove(top_bid)

    @staticmethod
    @try_exc_regular
    def _append_format_pos(position):
        position.update({'timestamp': int(time.time()),
                         'entry_price': float(position['entryPrice']),
                         'amount': float(position['size']),
                         'amount_usd': float(position['size']) * float(position['entryPrice'])})
        return position

    @try_exc_regular
    def _update_positions(self, positions):
        for position in positions:
            position = self._append_format_pos(position)
            self.positions.update({position['market']: position})
            # position_example = [{'id': '312711e6-d172-5e5b-9dc8-362101e94756',
            # 'accountId': 'f47ae945-06ae-5c47-aaad-450c0ffc6164', 'market': 'SNX-USD',
            # 'side': 'LONG/SHORT',
            # 'status': 'OPEN', 'size': '13129.1', 'maxSize': '25107', 'entryPrice': '2.363965',
            # 'exitPrice': '2.398164', 'openTransactionId': '110960769',
            # 'closeTransactionId': None, 'lastTransactionId': '114164888', 'closedAt': None,
            # 'updatedAt': '2022-10-11T00:50:34.217Z', 'createdAt': '2022-10-11T00:50:34.217Z',
            # 'sumOpen': '219717.4', 'sumClose': '206588.3', 'netFunding': '706.266653',
            # 'realizedPnl': '7771.372704'}]

    # def get_pnl(self):
    #     try:
    #         position = self.get_positions()
    #     except:
    #         return 0
    #     realized_pnl = float(position['realizedPnl'])
    #     entry_price = float(position['entryPrice'])
    #     size = float(position['size'])
    #     ob = self.get_orderbook()
    #     index_price = (ob['asks'][0][0] + ob['bids'][0][0]) / 2
    #     unrealized_pnl = size * (index_price - entry_price)
    #     return unrealized_pnl + realized_pnl

    @try_exc_regular
    def get_positions(self):
        # NECESSARY
        return self.positions

    @try_exc_regular
    def _update_orders(self, orders):
        # print('WS resp:\n', orders, '\n\n')
        for order in orders:
            # print(f'DYDX websocket update {order=}')
            status = None
            if order['status'] == ClientsOrderStatuses.PENDING:
                if self.orders.get(order['id']):
                    continue
                else:
                    status = OrderStatus.PROCESSING
            if order['status'] == ClientsOrderStatuses.FILLED:
                status = OrderStatus.FULLY_EXECUTED
            elif order['status'] == ClientsOrderStatuses.CANCELED and order['remainingSize'] != order['size']:
                status = OrderStatus.PARTIALLY_EXECUTED
            elif order['status'] == ClientsOrderStatuses.CANCELED and order['remainingSize'] == order['size']:
                status = OrderStatus.NOT_EXECUTED

            if status:
                if status in [OrderStatus.PROCESSING, OrderStatus.NOT_EXECUTED]:
                    real_price = 0
                    executed_size = 0
                    executed_size_usd = 0
                else:
                    real_price = self.get_last_price(order['market'], order['side'])
                    executed_size = float(order['size']) - float(order['remainingSize'])
                    executed_size_usd = executed_size * real_price
                result = {
                    'exchange_order_id': order['id'],
                    'exchange': self.EXCHANGE_NAME,
                    'status': status,
                    'factual_price': real_price,
                    'factual_amount_coin':  executed_size,
                    'factual_amount_usd': executed_size_usd,
                    'datetime_update': datetime.utcnow(),
                    'ts_update': int(time.time() * 1000)
                }
                self.orders.update({order['id']: result})

            # if self.orders.get(order['market']):
            #     if not order['status'] in ['CANCELED', 'FILLED']:
            #         self.orders[order['market']].update({order['id']: order})
            #         time_create = self.timestamp_from_date(order['createdAt'])
            #         print(f"DYDX ORDER PLACE TIME: {time_create - self.time_sent} sec")
            #     else:
            #         if self.orders.get(order['id']):
            #             self.orders.pop(order['id'])
            # else:
            #     # print(f"DYDX _UPDATE_ORDER: {order}")
            #     # time_create = self.timestamp_from_date(order['createdAt'])
            #     # print(f"DYDX ORDER PLACE TIME: {time_create - self.time_sent} sec")
            #     self.orders.update({order['market']: {order['id']: order}})
            # order_example = [{'id': '28c21ee875838a5e349cf96d678d8c6151a250f979d6a025b3f79dcca703558',
            # 'clientId': '7049071120643888', 'market': 'SNX-USD',
            # 'accountId': 'f47ae945-06ae-5c47-aaad-450c0ffc6164', 'side': 'SELL', 'size': '483.3',
            # 'remainingSize': '0', 'limitFee': '0.0008', 'price': '2.47', 'triggerPrice': None,
            # 'trailingPercent': None, 'type': 'LIMIT', 'status': 'FILLED/OPEN/PENDING/CANCELED',
            # 'signature': '',
            # 'timeInForce': 'GTT', 'postOnly': False, 'cancelReason': None,
            # 'expiresAt': '2022-11-04T13:11:20.000Z', 'unfillableAt': '2022-11-03T13:18:00.185Z',
            # 'updatedAt': '2022-11-03T13:18:00.185Z', 'createdAt': '2022-11-03T13:18:00.148Z',
            # 'reduceOnly': False, 'country': 'JP', 'client': None, 'reduceOnlySize': None}]

    @staticmethod
    @try_exc_regular
    def timestamp_from_date(date: str):
        # date = '2023-02-15T02:55:27.640Z'
        ms = int(date.split(".")[1].split('Z')[0]) / 1000
        return time.mktime(datetime.strptime(date, "%Y-%m-%dT%H:%M:%S.%fZ").timetuple()) + ms

    @try_exc_regular
    def get_orders(self):
        # NECESSARY
        return self.orders

    @staticmethod
    @try_exc_regular
    def __update_fill(accumulated_fills, fill):
        old_size = accumulated_fills[fill['market']]['size']
        old_price = accumulated_fills[fill['market']]['price']
        old_fee = accumulated_fills[fill['market']]['fee']

        new_size = old_size + float(fill['size'])
        new_price = (old_price * old_size + float(fill['price']) * float(fill['size'])) / (new_size)
        new_fee = old_fee + float(fill['fee'])

        accumulated_fills[fill['market']]['size'] = new_size
        accumulated_fills[fill['market']]['price'] = new_price
        accumulated_fills[fill['market']]['fee'] = new_fee
        return accumulated_fills

    @try_exc_regular
    def _update_fills(self, fills):
        accumulated_fills = {}
        for fill in fills:
            if not accumulated_fills.get(fill['market']):
                for key in ['fee', 'price', 'size']:
                    fill[key] = float(fill[key])
                accumulated_fills.update({fill['market']: fill})
            else:
                accumulated_fills = self.__update_fill(accumulated_fills, fill)
        for market, fill in accumulated_fills.items():
            if self.fills.get(market):
                self.fills[market].insert(0, fill)
            else:
                self.fills.update({market: [fill]})
        # example = [{'market': 'SNX-USD', 'transactionId': '114163898', 'quoteAmount': '17.29',
        # 'price': '2.470000', 'size': '7', 'liquidity': 'TAKER',
        # 'accountId': 'f47ae945-06ae-5c47-aaad-450c0ffc6164', 'side': 'SELL',
        # 'orderId': '28c21ee875838a5e349cf96d678d8c6151a250f979d6a025b3f79dcca703558',
        # 'fee': '0.004599', 'type': 'LIMIT', 'id': 'b6252559-f7c2-5ad5-afb5-0e33144ccdfc',
        # 'nonce': None, 'forcePositionId': None, 'updatedAt': '2022-11-03T13:18:00.185Z',
        # 'createdAt': '2022-11-03T13:18:00.185Z', 'orderClientId': '7049071120643888'}]

    @try_exc_regular
    def get_fills(self):
        return self.fills

    @try_exc_regular
    def get_balance(self):
        # NECESSARY
        if time.time() - self.balance['timestamp'] > 60:
            self.get_real_balance()
        return self.balance['total']

    @try_exc_regular
    def _update_account(self, account):
        self.balance = {'free': float(account['freeCollateral']),
                        'total': float(account['equity']),
                        'timestamp': time.time()}
        for market, position in account['openPositions'].items():
            position = self._append_format_pos(position)
            self.positions[market] = position

    @try_exc_regular
    def get_last_price(self, market, side):
        side = side.upper()
        last_trade = self.get_fills()
        last_price = 0
        if last_trade.get(market):
            last_trade = last_trade[market][0]
            if last_trade['side'] == side:
                last_price = last_trade['price']
        return last_price

    # example = {'starkKey': '03124cf5bb8e07d4a5d05cd2d6f79a13f4c370130296df9698210dbec21d927a',
    #    'positionId': '208054', 'equity': '71276.226361', 'freeCollateral': '63848.633515',
    #    'pendingDeposits': '0.000000', 'pendingWithdrawals': '0.000000', 'openPositions': {
    # 'SNX-USD': {'market': 'SNX-USD', 'status': 'OPEN', 'side': 'LONG', 'size': '13438.1',
    #             'maxSize': '25107', 'entryPrice': '2.363881', 'exitPrice': '2.397996',
    #             'unrealizedPnl': '1506.202651', 'realizedPnl': '7737.476749',
    #             'createdAt': '2022-10-11T00:50:34.217Z', 'closedAt': None,
    #             'sumOpen': '219543.1', 'sumClose': '206105.0', 'netFunding': '706.266653'},
    # 'ETH-USD': {'market': 'ETH-USD', 'status': 'OPEN', 'side': 'SHORT', 'size': '-10.688',
    #             'maxSize': '-25.281', 'entryPrice': '1603.655165',
    #             'exitPrice': '1462.275353', 'unrealizedPnl': '749.364703',
    #             'realizedPnl': '8105.787595', 'createdAt': '2022-08-16T22:56:10.625Z',
    #             'closedAt': None, 'sumOpen': '71.478', 'sumClose': '60.790',
    #             'netFunding': '-488.691199'}}, 'accountNumber': '0',
    #    'id': 'f47ae945-06ae-5c47-aaad-450c0ffc6164', 'quoteBalance': '87257.614961',
    #    'createdAt': '2022-08-16T18:52:16.881Z'}

    @try_exc_regular
    def get_available_balance(self):
        # NECESSARY
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

    # def get_available_balance(self, side):
    #     position_value = 0
    #     position_value_abs = 0
    #     for symbol, position in self.positions.items():
    #         if position.get('amount_usd'):
    #             position_value += position['amount_usd']
    #             position_value_abs += abs(position['amount_usd'])
    #     if position_value_abs > available_margin:
    #         if position_value > 0:
    #             if side == 'buy':
    #                 return available_margin - position_value
    #             elif side == 'sell':
    #                 return available_margin + position_value
    #         else:
    #             if side == 'buy':
    #                 return available_margin + abs(position_value)
    #             elif side == 'sell':
    #                 return available_margin - abs(position_value)
    #     if side == 'buy':
    #         return available_margin - position_value
    #     elif side == 'sell':
    #         return available_margin + position_value

    @try_exc_regular
    def _process_msg(self, msg: aiohttp.WSMessage):
        if msg.type == aiohttp.WSMsgType.TEXT:
            obj = json.loads(msg.data)
            if obj.get('channel'):
                if obj['channel'] == 'v3_orderbook':
                    self._updates += 1
                    if obj['type'] == 'subscribed':
                        self._first_orderbook_update(obj)
                        self.count_flag = True
                    elif obj['type'] == 'channel_data':
                        self._channel_orderbook_update(obj)
                elif obj['channel'] == 'v3_accounts':
                    if obj['contents'].get('positions'):
                        if len(obj['contents']['positions']):
                            self._update_positions(obj['contents']['positions'])
                    if obj['contents'].get('fills'):
                        if len(obj['contents']['fills']):
                            self._update_fills(obj['contents']['fills'])
                    if obj['contents'].get('orders'):
                        if len(obj['contents']['orders']):
                            self._update_orders(obj['contents']['orders'])
                    if obj['contents'].get('account'):
                        if len(obj['contents']['account']):
                            self._update_account(obj['contents']['account'])

    @try_exc_async
    async def get_orderbook_by_symbol(self, symbol):
        # NECESSARY
        async with aiohttp.ClientSession() as session:
            data = {}
            now_iso_string = generate_now_iso()
            request_path = f'/v3/orderbook/{symbol}'
            signature = self.client.private.sign(
                request_path=request_path,
                method='GET',
                iso_timestamp=now_iso_string,
                data=remove_nones(data),
            )

            headers = {
                'DYDX-SIGNATURE': signature,
                'DYDX-API-KEY': self.API_KEYS['key'],
                'DYDX-TIMESTAMP': now_iso_string,
                'DYDX-PASSPHRASE': self.API_KEYS['passphrase']
            }

            async with session.get(url=self.BASE_URL + request_path, headers=headers,
                                   data=json.dumps(remove_nones(data))) as resp:
                res = await resp.json()
                if 'asks' in res and 'bids' in res:
                    orderbook = {
                        'asks': [[float(x['price']), float(x['size'])] for x in res['asks']],
                        'bids': [[float(x['price']), float(x['size'])] for x in res['bids']],
                        'timestamp': int(time.time() * 1000)
                    }
                return orderbook

    @try_exc_regular
    def get_orderbook(self, symbol):
        # NECESSARY
        while not self.orderbook.get(symbol):
            print(f"{self.EXCHANGE_NAME}: CAN'T GET OB {symbol}")
            time.sleep(0.01)
        return self.orderbook[symbol]


#


# import configparser
# import sys
#
# cp = configparser.ConfigParser()
# if len(sys.argv) != 2:
#     sys.exit(1)
# cp.read(sys.argv[1], "utf-8")
# dydx_keys = cp['DYDX']
# client = DydxClient(dydx_keys)
# client.run_updater()
# #
# async def create_order(amount, price, side, client):
#     return await client.create_order(amount, price, side, client._loop)
#
# while True:
#     time.sleep(1)
#     client.client.private.cancel_all_orders()
#     time.sleep(2)
#     create_order(0.01, 1000000, 'sell', client._loop)
# time.sleep(2)
# print(f"BUY {client.get_last_price('buy')}")
# client.create_order(0.01, 15000, 'sell')
# time.sleep(2)
# print(f"SELL {client.get_last_price('sell')}")
# time.sleep(2)
# while True:
#     time.sleep(0.001)
#     orderbook = client.get_orderbook()[client.symbol]
#     print(f"TOP ASK:\nPrice:{orderbook['asks'][0][0]}\nSize: {orderbook['asks'][0][1]}\n")
#     print(f"TOP BID:\nPrice:{orderbook['bids'][0][0]}\nSize: {orderbook['bids'][0][1]}\n\n")
#     print(f"BUY {client.get_available_balance('buy')}")
#     print(f"SELL {client.get_available_balance('sell')}")
#     print()
#     print()
#     time.sleep(2)
#
# orders_response = client.client.private.get_orders(
#     market='BTC-USD',
#     status='UNTRIGGERED'
# ).data
# print(orders_response)
# client.create_order(amount=0.1, price=1000000, side='SELL', type='LIMIT')
# #

#     print(client.get_orderbook())
#     a = client.orders
#     print(a)

# #     balance_DYDX = client.client.private.get_account().data
# #     print(client.orderbook)
# #     print(balance_DYDX)
# #     print()
# client_pub = Client(host=API_HOST_MAINNET)
# average_dydx = []
# average_bitmex = []
# client.run_updater()
#
# while True:
#     orderbook = client_pub.public.get_orderbook(market='BTC-USD').data
#     print(orderbook)
#     print(client.orderbook)
#     print()
#     print()

if __name__ == '__main__':
    import configparser
    import sys

    config = configparser.ConfigParser()
    config.read('config.ini', "utf-8")
    client = DydxClient(keys=config['DYDX'],
                        leverage=float(config['SETTINGS']['LEVERAGE']),
                        max_pos_part=int(config['SETTINGS']['PERCENT_PER_MARKET']),
                        markets_list=['RUNE'])
    client.run_updater()
    client.get_available_balance()
    # client.get_real_balance()
    # print(client.get_balance())
    # client.run_updater()
    fills = client.client.private.get_fills().data#market='1INCH-USD')
    for fill in fills['fills'][::-1]:
        print(f"MARKET: {fill['market']}")
        print(f"TIME: {fill['createdAt']}")
        print(f"SIDE: {fill['side']}")
        print(f"PRICE: {fill['price']}")
        print(f"SIZE: {fill['size']}")
        print(f"LIQUIDITY: {fill['liquidity']}\n")
    # time.sleep(3)
    # print(client.get_balance())
    # print()
    # print(client.positions)
    # print()
    # print(client.new_get_available_balance())


    # async def test_order():
    #     async with aiohttp.ClientSession() as session:
    #         ob = client.get_orderbook('RUNE-USD')
    #         price = ob['asks'][5][0]
    #         # client.get_markets()
    #         client.fit_sizes(10, price, 'RUNE-USD')
    #         data = await client.create_order('RUNE-USD',
    #                                          'buy',
    #                                          session=session,
    #                                          client_id=f"api_deal_{str(uuid.uuid4()).replace('-', '')[:20]}")
    #         # print(data)
    # #         print(data)
    # #         client.cancel_all_orders()
    # #
    # time.sleep(5)
    # asyncio.run(test_order())
    # while True:
    #     time.sleep(5)
    #     print(client.get_all_tops())
    #     print(client.get_balance())
    # while True:
    #     time.sleep(5)
    #     asyncio.run(test_order())
