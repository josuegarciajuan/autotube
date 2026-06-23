"""Proxy manager for routing YouTube API calls through residential IPs.

Supports SOCKS5 (SSH tunnel) and HTTP proxies.  Configured via
environment variables:

    PROXY_ENABLED=true
    PROXY_TYPE=socks5          # socks5 | http
    PROXY_HOST=127.0.0.1
    PROXY_PORT=1080
    PROXY_CHANNELS=canal2      # comma-separated, empty = all
"""

import logging
import httplib2

logger = logging.getLogger(__name__)


def configure_proxy_for_http(
    http: httplib2.Http,
    proxy_type: str,
    proxy_host: str,
    proxy_port: int,
    proxy_user: str = None,
    proxy_pass: str = None,
) -> None:
    """Configure an httplib2.Http instance to use a proxy.

    Args:
        http: The httplib2.Http instance to configure.
        proxy_type: 'socks5' or 'http'.
        proxy_host: Proxy server hostname or IP.
        proxy_port: Proxy server port.
        proxy_user: Optional proxy username.
        proxy_pass: Optional proxy password.
    """
    proxy_type_lower = proxy_type.lower()

    if proxy_type_lower == "socks5":
        proxy_info = httplib2.ProxyInfo(
            proxy_type=httplib2.socks.PROXY_TYPE_SOCKS5,
            proxy_host=proxy_host,
            proxy_port=proxy_port,
            proxy_user=proxy_user,
            proxy_pass=proxy_pass,
        )
    elif proxy_type_lower in ("http", "https"):
        proxy_info = httplib2.ProxyInfo(
            proxy_type=httplib2.socks.PROXY_TYPE_HTTP,
            proxy_host=proxy_host,
            proxy_port=proxy_port,
            proxy_user=proxy_user,
            proxy_pass=proxy_pass,
        )
    else:
        logger.warning("Unknown proxy type '%s' — proxy NOT configured", proxy_type)
        return

    http.proxy_info = proxy_info
    logger.debug("Proxy configured: %s://%s:%s", proxy_type, proxy_host, proxy_port)


def should_use_proxy(channel_slug: str) -> bool:
    """Determine if a given channel should route through a proxy.

    Args:
        channel_slug: The channel slug (e.g. 'canal2').

    Returns:
        True if proxy should be used for this channel.
    """
    from config.settings import PROXY_ENABLED, PROXY_CHANNELS

    if not PROXY_ENABLED:
        return False
    if not PROXY_CHANNELS:
        return True  # empty list = apply to ALL channels
    return channel_slug in PROXY_CHANNELS
