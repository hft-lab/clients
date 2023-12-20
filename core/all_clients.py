from clients.apollox import ApolloxClient
from clients.binance import BinanceClient
from clients.bitmex import BitmexClient
from clients.dydx import DydxClient
from clients.kraken import KrakenClient
from clients.okx import OkxClient
from clients.hitbtc import HitbtcClient

ALL_CLIENTS = {
    'BITMEX': BitmexClient,
    'DYDX': DydxClient,
    'BINANCE': BinanceClient,
    'APOLLOX': ApolloxClient,
    'OKX': OkxClient,
    'KRAKEN': KrakenClient,
    'HITBTC': HitbtcClient
}
