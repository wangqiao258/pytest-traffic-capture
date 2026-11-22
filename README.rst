pytest-traffic-capture
======================

在 pytest 运行期间捕获 ``requests``、``httpx`` 和 ``aiohttp`` 的流量，并将其写为
HAR 1.2 文档以及可重放的 cURL 命令。

功能特性
--------

* 捕获使用 ``requests``、``httpx`` （同步和异步）以及 ``aiohttp`` 进行的 HTTP
  交换。
* 每个测试写入一个 ``.har`` 文件和一个 ``.curl.txt`` 文件。
* 将较大的 body 移到 sidecar 文件，而不是撑大 HAR 文档。
* 当调用方通过普通的 ``read``/``iter_content``/``iter_bytes`` API 消费 streaming
  响应时，惰性地记录它们。
* 对 307/308 重定向跳转复用捕获的负载，使重放保留原始字节。
* 通过使用 sidecar 和 ``--data-binary``，对非 UTF-8 请求 body 强制进行字节精确的
  cURL 重放。

环境要求
------------

* Python >= 3.9
* pytest >= 8
* 被测项目中至少包含 ``requests``、``httpx`` 或 ``aiohttp`` 之一

安装
------------

从项目目录安装插件::

    pip install -e .

用于开发（包含测试依赖）::

    pip install -e ".[test]"

用法
-----

运行 pytest 并加上 ``--capture-traffic`` 以启用捕获::

    pytest --capture-traffic

捕获默认处于禁用状态。可用选项：

``--capture-dir DIR``
    输出目录。默认为相对于 pytest 根目录的 ``captured_traffic``。

``--capture-max-inline-bytes N``
    保留在 HAR 文件中内联的最大 body 大小。更大的 body 会写入 ``bodies/``。
    默认为 1 MiB。``0`` 会取消对文本请求 body 和响应 body 的大小限制。二进制和
    multipart 请求 body 始终写入 sidecar 文件，以便 cURL 能够重放完全一致的字节。

``--capture-max-body-bytes N``
    每个请求或响应 body 捕获的最大字节数。更大的 body 会在产物中被截断，并标记
    ``_truncated`` 和 ``_size``；cURL 重放会被标注为不完整。默认为 ``0`` （无限制）。

同样的设置也可以在 ``pytest.ini`` 中配置::

    [pytest]
    capture_traffic_dir = captured_traffic
    capture_traffic_max_inline_bytes = 1048576
    capture_traffic_max_body_bytes = 0

输出
------

::

    captured_traffic/
    |-- traffic_tests_test_api.py_test_login_ab12cd34.har
    |-- traffic_tests_test_api.py_test_login_ab12cd34.curl.txt
    `-- bodies/

.. warning::

   只要安装了本插件，捕获目录就会被排除在 pytest 收集之外，即使没有传入
   ``--capture-traffic`` 也是如此。请为 ``--capture-dir`` 使用一个专用的子目录：
   不支持将其指向项目根目录（``.``）或包含真实测试的目录。产物名称以 ``traffic_``
   开头，因此不会匹配 doctest 默认的 ``test*.txt`` glob。旧版本遗留的产物会保留其
   旧名称；升级后请删除一次。

HAR 文件可以导入 Chrome DevTools 的 Network 面板。名称以 ``_`` 开头的字段（例如
``_file``、``_streaming``、``_truncated``、``_redirects_truncated``、
``_headers_synthesized`` 和 ``_wireFileName``）是用于 sidecar 引用和捕获元数据的
HAR 扩展字段；浏览器查看器会忽略它们。存储在 ``bodies/`` 中的 body 通过 ``_file``
引用，浏览器 HAR 查看器不会显示它们；请打开 sidecar 文件来查看。

``.curl.txt`` 中的命令使用 POSIX shell 语法（bash/zsh/Git Bash），并通过生成它们的
机器上的绝对路径来引用 sidecar body。在 WSL 上，重放前请用 ``wslpath`` 改写这些路径。

已知限制
-----------------

* streaming body 通过普通的 ``read`` 和字节迭代器 API 收集。直接使用 httpx 的
  ``iter_raw()`` 或 ``requests`` 的 ``response.raw`` 会绕过收集。
* requests、httpx 和 aiohttp 的重定向链都会被记录。无法完成的链会标记
  ``_redirects_truncated``；从未到达传输层的请求会标记 ``_not_sent``。
* aiohttp 在传输时会对 multipart 文件名进行百分号编码。HAR 保留解码后的
  ``fileName`` 以便阅读，并将传输形式存储在 ``_wireFileName`` 中；cURL 重放使用
  传输形式。
* 当 aiohttp 在生成的请求上暴露编码后的 ``bodySize`` 时，aiohttp ``FormData``
  请求会报告实测的编码 ``bodySize``；否则回退为 ``-1``。
* ``requests`` 的生成器和不可 seek 的流式上传会在客户端消费时通过 tee 捕获。如果
  客户端提前停止，捕获的 body 会标记 ``_truncated``，重放不完整。
* 从 ``requests`` 响应钩子发起的嵌套请求会被捕获；重定向跳转由外层调用记录一次。
  显式传入 ``allow_redirects=False`` 的嵌套请求与重定向跳转无法区分，不会单独记录。
* 包含 ``=``、``;``、引号或控制字符的 multipart 字段名无法通过 curl 的 ``-F``
  表达；这些请求改用原始 multipart body 和原始 ``Content-Type`` 头来重放。
* curl 在重放时会规范化 multipart Content-Type 参数值（例如 ``charset="utf-8"``）；
  HAR 保留完全一致的头。
* 非 ASCII 请求 body 和表单字段通过 sidecar 文件重放。从 Git Bash 调用时，原生
  Windows ``curl`` 仍可能对非 ASCII 的 sidecar 路径或 multipart 文件名编码错误；
  请优先使用 ASCII 捕获目录，或设置 ``MSYS2_ARG_CONV_EXCL='*'``。
* 在 Windows Git Bash 上，MSYS 路径转换可能改写看起来像 POSIX 路径的参数（例如
  ``@/etc/passwd``）；重放此类捕获命令时请设置 ``MSYS2_ARG_CONV_EXCL='*'``。
* 生成的 cURL 命令会为捕获的 body 省略 ``Content-Length``，以便 curl 重新计算它。
  没有 body 的请求可能仍保留 ``Content-Length: 0``。
* HAR ``timings`` 将实测总时长报告为 ``wait``：requests、httpx 和 aiohttp 都不暴露
  首字节时间或传输时长。httpx/aiohttp 记录的重定向跳转共享相同的起始时间，只有最后
  一跳携带实测时长。
* ``pytest`` 9.2.0 在 Python 3.10.0 上收集时会因上游 ``NodeId`` 错误而失败；请在该
  解释器上固定 ``pytest<9.2``。即使没有本插件也会发生此失败。
* 捕获的 streaming body、生成器上传和可 seek 的上传快照会在内存中缓冲，直到测试
  结束。设置 ``--capture-max-body-bytes`` 可限制捕获的部分；客户端仍会收到完整
  body，但产物会被标记为截断，cURL 重放不完整。
* aiohttp 错误条目会重建 aiohttp 自动添加的头（``Accept``、``Accept-Encoding``、
  ``User-Agent``、``Host`` 和 cookies），并标记 ``_headers_synthesized``。

.. warning::

   捕获的产物包含请求和响应头、cookies、查询字符串和 body。它们可能包含凭据或个人
   数据。请勿将 ``captured_traffic/`` 提交到版本控制。文件按进程 umask 创建；在多
   用户主机上，请限制对捕获目录的访问。

开发
-----------

运行测试套件::

    pip install tox
    tox

测试套件包含一个本地 HTTP 服务器、重定向/上传/下载场景以及 cURL 重放测试（当
``bash`` 不可用时会跳过重放）。``aiohttp39`` 环境用于检查与 aiohttp 3.9 的兼容性。

许可证
-------

基于 MIT 许可证条款分发。