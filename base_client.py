import aiohttp
from abc import ABC, abstractmethod


class BaseClient(ABC):
    BASE_URL = None
    BASE_WS = None
    EXCHANGE_NAME = None
    LAST_ORDER_ID = 'default'

    @abstractmethod
    def get_available_balance(self):
        """
        Amount available to trade in certain direction in USD

        :param side: SELL/BUY side for check balance
        :return: float
        """
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

    @abstractmethod
    async def create_order(self, price: float, side: str,
                           session: aiohttp.ClientSession, expire: int = 100, client_ID: str = None) -> dict:
        """
        Create order func

        :param price: SELL/BUY price
        :param side: SELL/BUY in lowercase
        :param order_type: LIMIT or MARKET
        :param session: session from aiohttpClientSession
        :param expire: int value for exp of order
        :param client_ID:
        :return:
        """
        pass

    @abstractmethod
    def cancel_all_orders(self, orderID=None) -> dict:
        """
        cancels all orders by symbol or orderID
        :return: response from exchange api
        """
        pass

    @abstractmethod
    def get_positions(self) -> dict:
        pass

    @abstractmethod
    def get_real_balance(self) -> float:
       pass

    @abstractmethod
    def get_orderbook(self) -> dict:
        pass

    @abstractmethod
    def get_last_price(self, side: str) -> float:
        pass

    async def get_all_orders(self, symbol: str, session: aiohttp.ClientSession) -> list:
        return []
