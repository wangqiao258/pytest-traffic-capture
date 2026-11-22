pytest-traffic-capture
======================

在 pytest 运行期间捕获 ``requests``、``httpx`` 和 ``aiohttp`` 的流量，并将其写为
HAR 1.2 文档。

功能特性
--------

* 捕获使用 ``requests``、``httpx`` （同步和异步）以及 ``aiohttp`` 进行的 HTTP
  交换。
* 每个测试写入一个 ``.har`` 文件。
* 将较大的 body 移到 sidecar 文件，而不是撑大 HAR 文档。
* 当调用方通过普通的 ``read``/``iter_content``/``iter_bytes`` API 消费 streaming
  响应时，惰性地记录它们。
* 对 307/308 重定向跳转复用捕获的负载，使每一跳都保留原始字节。

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
    默认为 1 MiB。``0`` 会取消对文本请求 body 和响应 body 的大小限制。二进制请求
    body 和 multipart 文件部分始终写入 sidecar 文件。

``--capture-max-body-bytes N``
    每个请求或响应 body 捕获的最大字节数。更大的 body 会在产物中被截断，并标记
    ``_truncated`` 和 ``_size``。默认为 5 MiB，
    ``0`` 表示不限制。请求头和响应头始终完整保存，只有 body 会被截断；截断后的
    产物只包含请求头和已捕获的那部分 body。

同样的设置也可以在 ``pytest.ini`` 中配置::

    [pytest]
    capture_traffic_dir = captured_traffic
    capture_traffic_max_inline_bytes = 1048576
    capture_traffic_max_body_bytes = 5242880

输出
------

::

    captured_traffic/
    |-- traffic_tests_test_api.py_test_login_ab12cd34.har
    `-- bodies/

.. warning::

   只要安装了本插件，捕获目录就会被排除在 pytest 收集之外，即使没有传入
   ``--capture-traffic`` 也是如此。请为 ``--capture-dir`` 使用一个专用的子目录：
   不支持将其指向项目根目录（``.``）或包含真实测试的目录。产物名称以 ``traffic_``
   开头，因此不会匹配 doctest 默认的 ``test*.txt`` glob。

HAR 文件可以导入 Chrome DevTools 的 Network 面板，也可以导入 Postman 等工具。
名称以 ``_`` 开头的字段（例如
``_file``、``_streaming``、``_truncated``、``_redirects_truncated`` 和
``_headers_synthesized``）是用于 sidecar 引用和捕获元数据的
HAR 扩展字段；浏览器查看器会忽略它们。存储在 ``bodies/`` 中的 body 通过 ``_file``
引用，浏览器 HAR 查看器不会显示它们；请打开 sidecar 文件来查看。

有意设计
-------------

以下行为是刻意采用的保守策略，用于如实反映线上流量，不是缺陷：

* ``requests`` 的生成器和不可 seek 的流式上传会在客户端消费时通过 tee 捕获。如果
  客户端提前停止，捕获的 body 会标记 ``_truncated``，产物不完整；此时线上确实没有
  发出完整字节。
* aiohttp 错误条目会重建 aiohttp 自动添加的头（``Accept``、``Accept-Encoding``、
  ``User-Agent``、``Host`` 和 cookies），并标记 ``_headers_synthesized``。

已知限制
-----------------

* streaming body 通过普通的 ``read`` 和字节迭代器 API 收集。直接使用 httpx 的
  ``iter_raw()`` 或 ``requests`` 的 ``response.raw`` 会绕过收集。
* requests、httpx 和 aiohttp 的重定向链都会被记录。无法完成的链会标记
  ``_redirects_truncated``；从未到达传输层的请求会标记 ``_not_sent``。
* aiohttp 在传输时会对 multipart 文件名进行百分号编码；HAR 保留解码后的
  ``fileName`` 以便阅读。
* 当 aiohttp 在生成的请求上暴露编码后的 ``bodySize`` 时，aiohttp ``FormData``
  请求会报告实测的编码 ``bodySize``；否则回退为 ``-1``。
* 从 ``requests`` 响应钩子发起的嵌套请求会被捕获；重定向跳转由外层调用记录一次。
  显式传入 ``allow_redirects=False`` 的嵌套请求与重定向跳转无法区分，不会单独记录。
* HAR ``timings`` 将实测总时长报告为 ``wait``：requests、httpx 和 aiohttp 都不暴露
  首字节时间或传输时长。httpx/aiohttp 记录的重定向跳转共享相同的起始时间，只有最后
  一跳携带实测时长。
* ``pytest`` 9.2.0 在 Python 3.10.0 上收集时会因上游 ``NodeId`` 错误而失败；请在该
  解释器上固定 ``pytest<9.2``。即使没有本插件也会发生此失败。
* 捕获的 streaming body、生成器上传和可 seek 的上传快照会在内存中缓冲，直到测试
  结束。``--capture-max-body-bytes`` 默认把每个 body 的缓冲限制在 5 MiB；客户端
  仍会收到完整 body，但产物会被标记为截断。非流式调用（例如
  ``requests.get`` 不开 ``stream=True``）的内存开销由 HTTP 库本身决定，本插件无法
  降低。截断时终端摘要会提示被截断的 body 数量。

.. warning::

   捕获的产物包含请求和响应头、cookies、查询字符串和 body。它们可能包含凭据或个人
   数据。请勿将 ``captured_traffic/`` 提交到版本控制。文件按进程 umask 创建；在多
   用户主机上，请限制对捕获目录的访问。

开发
-----------

运行测试套件::

    pip install tox
    tox

测试套件包含一个本地 HTTP 服务器以及重定向/上传/下载场景。``aiohttp39`` 环境用于
检查与 aiohttp 3.9 的兼容性。

许可证
-------

基于 MIT 许可证条款分发。