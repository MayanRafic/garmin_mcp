"""Use Garmin's maintained session client while retaining the local API adapter."""

import base64

from garminconnect import Garmin as SDKGarmin


class Garmin(SDKGarmin):
    """Keep existing tool modules compatible with the SDK's renamed client."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Several existing tools and the desktop CLI use the old attribute.
        # This is the native DI session client; it never invokes garth OAuth1.
        self.garth = self.client


def encoded_session(client) -> str:
    """Encode the native JSON session for the host's existing secret setting."""
    return base64.b64encode(client.dumps().encode("utf-8")).decode("ascii")
