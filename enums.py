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
    SLIPPAGE = 'SLIPPAGE'



class ResponseStatus:
    SUCCESS = 'success'
    NO_CONNECTION = 'no_connection'
    ERROR = 'error'


class OrderStatus:
    NOT_EXECUTED = 'Not Executed'
    # DELAYED_FULLY_EXECUTED = 'Delayed Fully Executed'
    PARTIALLY_EXECUTED = 'Partially Executed'
    FULLY_EXECUTED = 'Fully Executed'
    PROCESSING = 'Processing'


class ClientsOrderStatuses:
    FILLED = 'FILLED'
    CANCELED = 'CANCELED'
    NEW = 'NEW'
    PARTIALLY_FILLED = 'PARTIALLY_FILLED'
    EXPIRED = 'EXPIRED'
    PENDING = 'PENDING'