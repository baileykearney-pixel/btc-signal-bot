"""
cTrader execution module using the official ctrader-open-api library.
Connects via TCP, authenticates app + account, places order, disconnects.
"""

import os
import logging
import threading
from twisted.internet import reactor, defer

log = logging.getLogger("cTraderExec")


def place_order_sync(signal: dict, account_id: str, access_token: str,
                     client_id: str, client_secret: str,
                     lots: float, ct_symbol: str) -> dict:
    """
    Synchronous wrapper around the async cTrader order placement.
    Blocks until order is placed or timeout. Returns result dict.
    """
    result = {"success": False, "error": None, "order_id": None}
    done_event = threading.Event()

    def run_in_reactor():
        try:
            from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
            from ctrader_open_api.messages.OpenApiMessages_pb2 import (
                ProtoOAApplicationAuthReq,
                ProtoOAAccountAuthReq,
                ProtoOANewOrderReq,
            )
            from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
                ProtoOAOrderType,
                ProtoOATradeSide,
            )

            client = Client(
                EndPoints.PROTOBUF_LIVE_HOST,
                EndPoints.PROTOBUF_PORT,
                TcpProtocol
            )

            state = {"app_authed": False, "account_authed": False}

            def onError(failure):
                result["error"] = str(failure)
                log.error(f"cTrader error: {failure}")
                done_event.set()
                try:
                    client.stopService()
                except Exception:
                    pass

            def onMessageReceived(client, message):
                msg = Protobuf.extract(message)
                msg_type = type(msg).__name__

                if msg_type == "ProtoOAApplicationAuthRes":
                    log.info("  cTrader: App authenticated")
                    state["app_authed"] = True
                    # Now auth the account
                    req = ProtoOAAccountAuthReq()
                    req.ctidTraderAccountId = int(account_id)
                    req.accessToken = access_token
                    d = client.send(req)
                    d.addErrback(onError)

                elif msg_type == "ProtoOAAccountAuthRes":
                    log.info("  cTrader: Account authenticated")
                    state["account_authed"] = True
                    # Place the order
                    trade_side = (
                        ProtoOATradeSide.Value("BUY")
                        if signal["direction"] == "LONG"
                        else ProtoOATradeSide.Value("SELL")
                    )
                    volume = int(lots * 100)  # Pepperstone: 1 lot = 1 BTC, volume in centilots

                    req = ProtoOANewOrderReq()
                    req.ctidTraderAccountId = int(account_id)
                    req.symbolName = ct_symbol
                    req.orderType = ProtoOAOrderType.Value("MARKET")
                    req.tradeSide = trade_side
                    req.volume = volume
                    req.stopLoss = round(signal["sl"], 5)
                    req.takeProfit = round(signal["tp"], 5)
                    req.comment = "BOS+OB Bot"

                    log.info(f"  Placing order: {signal['direction']} {ct_symbol} vol={volume} SL={signal['sl']:.4f} TP={signal['tp']:.4f}")
                    d = client.send(req)
                    d.addErrback(onError)

                elif msg_type == "ProtoOAExecutionEvent":
                    order_id = getattr(msg, "order", None)
                    if order_id:
                        order_id = getattr(order_id, "orderId", "unknown")
                    result["success"] = True
                    result["order_id"] = order_id
                    log.info(f"  ✅ Order executed! ID: {order_id}")
                    done_event.set()
                    try:
                        client.stopService()
                    except Exception:
                        pass

                elif msg_type == "ProtoOAErrorRes":
                    error_code = getattr(msg, "errorCode", "unknown")
                    description = getattr(msg, "description", "")
                    result["error"] = f"{error_code}: {description}"
                    log.error(f"  ❌ cTrader error: {error_code} — {description}")
                    done_event.set()
                    try:
                        client.stopService()
                    except Exception:
                        pass

            def connected(client):
                log.info("  cTrader: Connected")
                req = ProtoOAApplicationAuthReq()
                req.clientId = client_id
                req.clientSecret = client_secret
                d = client.send(req)
                d.addErrback(onError)

            def disconnected(client, reason):
                log.info(f"  cTrader: Disconnected — {reason}")
                done_event.set()

            client.setConnectedCallback(connected)
            client.setDisconnectedCallback(disconnected)
            client.setMessageReceivedCallback(onMessageReceived)
            client.startService()

        except Exception as e:
            result["error"] = str(e)
            done_event.set()

    # Run in reactor thread
    reactor.callFromThread(run_in_reactor)

    # Wait up to 30 seconds
    done_event.wait(timeout=30)

    if not done_event.is_set():
        result["error"] = "Timeout waiting for order execution"

    return result


def ensure_reactor_running():
    """Start Twisted reactor in background thread if not already running."""
    if not reactor.running:
        t = threading.Thread(target=reactor.run, kwargs={"installSignalHandlers": False})
        t.daemon = True
        t.start()
        import time
        time.sleep(0.5)  # give reactor time to start


def execute_trade(signal: dict, lots: float, ct_symbol: str) -> bool:
    """
    Main entry point. Call this to place a trade.
    Returns True if successful.
    """
    account_id = os.environ.get("CTRADER_ACCOUNT_ID", "")
    access_token = os.environ.get("CTRADER_ACCESS_TOKEN", "")
    client_id = os.environ.get("CTRADER_CLIENT_ID", "")
    client_secret = os.environ.get("CTRADER_CLIENT_SECRET", "")

    if not all([account_id, access_token, client_id, client_secret]):
        log.warning("cTrader credentials not set")
        return False

    ensure_reactor_running()

    result = place_order_sync(
        signal, account_id, access_token,
        client_id, client_secret, lots, ct_symbol
    )

    return result["success"], result.get("order_id"), result.get("error")
