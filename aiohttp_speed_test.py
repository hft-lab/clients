import time
import asyncio
import aiohttp

headers = {"Accept": "application/json;charset=UTF-8",
           "Content-Type": "application/json",
           'Connection': 'keep-alive'}


async def get_orderbook_btse(session):
    path = 'https://api.btse.com/futures/api/v2.1/orderbook'
    params = {'symbol': 'ETHPFC', 'depth': 10}
    post_string = '?' + "&".join([f"{key}={params[key]}" for key in sorted(params)])
    async with session.get(url=path + post_string, json=params) as resp:
        return resp.json()


async def get_orderbook_whitebit(session):
    path = 'https://whitebit.com/api/v4/public/orderbook/BTC_PERP'
    params = {'limit': 10}
    data = ''
    strl = []
    for key in sorted(params):
        strl.append(f'{key}={params[key]}')
    data += '&'.join(strl)
    path += f'?{data}'.replace(' ', '%20')
    async with session.get(url=path) as resp:
        return resp.json()


if __name__ == '__main__':
    whitebit_records = []
    btse_records = []

    async def test():
        async with aiohttp.ClientSession() as session:
            session.headers.update(headers)
            while True:
                time.sleep(1)

                try:
                    time_start_whitebit = time.time()
                    await get_orderbook_whitebit(session)
                    whitebit_records.append(time.time() - time_start_whitebit)
                    print(f"GET OB WHITEBIT AV. TIME: {sum(whitebit_records) / len(whitebit_records)} sec")

                    time_start_btse = time.time()
                    await get_orderbook_btse(session)
                    btse_records.append(time.time() - time_start_btse)
                    print(f"GET OB BTSE AV. TIME: {sum(btse_records) / len(btse_records)} sec")

                    print(f"ATTEMPTS: BTSE: {len(btse_records)} | WHITEBIT: {len(whitebit_records)}")
                    print()
                except:
                    pass

    asyncio.run(test())
