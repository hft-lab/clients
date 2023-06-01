class PositionSideEnum:
    LONG = 'LONG'
    SHORT = 'SHORT'
    BOTH = 'BOTH'

    @classmethod
    def all_position_sides(cls):
        return [cls.LONG, cls.SHORT, cls.BOTH]


class ConnectMethodEnum:
    PUBLIC = 'public'
    PRIVATE = 'private'


class EventTypeEnum:
    ACCOUNT_UPDATE = 'ACCOUNT_UPDATE'
    ORDER_TRADE_UPDATE = 'ORDER_TRADE_UPDATE'


class BotState:
    PARSER = 'PARSER'
    BOT = 'BOT'


class RabbitMqQueues:
    TELEGRAM = 'logger.event.send_message'

    # FOR BALANCING ALGORYTHM
    BALANCES = 'logger.event.insert_balances'
    BALANCE_DETALIZATION = 'logger.event.insert_balance_detalization'  # noqa
    DISBALANCE = 'logger.event.insert_disbalances'  # noqa
    ORDERS = 'logger.event.insert_orders'
    UPDATE_ORDERS = 'logger.event.update_orders'

    @staticmethod
    def get_exchange_name(routing_key: str):
        routing_list = routing_key.split('.')

        if len(routing_list) > 1 and ('periodic' in routing_key or 'event' in routing_key):
            return routing_list[0] + '.' + routing_list[1]

        raise f'Wrong routing key:{routing_key}'


class ResponseStatus:
    SUCCESS = 'success'
    NO_CONNECTION = 'no_connection'
    ERROR = 'error'


class OrderStatus:
    NOT_EXECUTED = 'Not Executed'
    DELAYED_FULLY_EXECUTED = 'Delayed Fully Executed'
    PARTIALLY_EXECUTED = 'Partially Executed'
    INSTANT_FULLY_EXECUTED = 'Instant Fully Executed'
    PROCESSING = 'Processing'


class ClientsOrderStatuses:
    FILLED = 'FILLED'
    CANCELED = 'CANCELED'
    NEW = 'NEW'
    PARTIALLY_FILLED = 'PARTIALLY_FILLED'
    EXPIRED = 'EXPIRED'
