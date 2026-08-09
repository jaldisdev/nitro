# nitro-intercom

Publish/subscribe channels for Python services, backed by the same Rust core
the Nitro framework uses internally.

Install this package only if you are **not** running on Nitro. Nitro projects
should import `nitro.intercom`, which reaches the same core in-process and
takes its configuration from the project's settings object instead of a
separate config surface.

```python
from nitro_intercom import Intercom

intercom = Intercom("redis://localhost:6379")
await intercom.publish("room:42", {"event": "joined", "user": "ada"})

async for message in intercom.subscribe("room:42"):
    print(message)
```
