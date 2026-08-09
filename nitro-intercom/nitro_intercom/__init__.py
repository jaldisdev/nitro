"""Publish/subscribe channels for Python services.

Install this package only if you are **not** running on Nitro. A Nitro project
should import ``nitro.intercom``, which reaches the same core in-process and
takes its configuration from the project's settings.

A channel is a plain string agreed on by convention — there is no registry and
no discovery. Messages are ordinary Python values, encoded as MessagePack on
the wire so a service written in another language can read them.

    from nitro_intercom import Intercom

    intercom = await Intercom.connect("redis://localhost:6379")
    await intercom.publish("room:42", {"event": "joined", "user": "ada"})

    listener = await intercom.subscribe("room:42")
    async for message in listener:
        print(message)
"""

from nitro_intercom._intercom import Intercom, Listener, Reader

__version__ = "0.1.0"

__all__ = ["Intercom", "Listener", "Reader", "__version__"]
