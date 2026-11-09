"""dsh-pet 的 Hermes 插件入口。

Hermes 只认插件根目录这一层的 ``__init__.py`` + ``plugin.yaml``，所以入口在这里；
适配逻辑都在 ``hermes_adapter`` 包里，桌宠本体仍然是 ``dsh-pet/`` 那份 Node 运行时。
"""

from .hermes_adapter import register

__all__ = ["register"]