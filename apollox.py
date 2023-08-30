from clients.binance import BinanceClient


class ApolloxClient(BinanceClient):
    BASE_WS = 'wss://fstream.apollox.finance/ws/'
    BASE_URL = 'https://fapi.apollox.finance'
    EXCHANGE_NAME = 'APOLLOX'

    def __init__(self, keys, leverage):
        super().__init__(keys, leverage)


if __name__ == '__main__':
    import time
    import aiohttp
    import uuid
    import asyncio
    import configparser
    import sys

    config = configparser.ConfigParser()
    config.read(sys.argv[1], "utf-8")
    client = ApolloxClient(config['APOLLOX'], config['LEVERAGE'])
    client.run_updater()
    time.sleep(5)

    async def test_order():
        async with aiohttp.ClientSession() as session:
            client.fit_amount(0.017)
            price = client.get_orderbook()[client.symbol]['bids'][10][0]
            data = await client.create_order(price, 'buy', session, client_id=uuid.uuid4())
            # data = await client.get_orderbook_by_symbol()
            print(data)
            client.cancel_all_orders()


    asyncio.run(test_order())

    while True:
        time.sleep(5)
        asyncio.run(test_order())
