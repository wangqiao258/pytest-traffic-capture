pytest-traffic-capture
======================

在 pytest 运行期间捕获 ``requests``、``httpx`` 和 ``aiohttp`` 的流量，并将其写为
HAR 1.2 文档。

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

每份 HAR 的 ``log._test`` 记录 ``nodeid``（原文）和 ``exchanges``（交换次数）。
文件名是 nodeid 的清洗 + 截断 + 短哈希投影，不可逆；需要精确对号时读这个字段。
以 ``_`` 开头的字段是本插件的扩展字段，HAR 查看器会忽略。

.. warning::

   只要安装了本插件，捕获目录就会被排除在 pytest 收集之外，即使没有传入
   ``--capture-traffic`` 也是如此。请为 ``--capture-dir`` 使用一个专用的子目录：
   不支持将其指向项目根目录（``.``）或包含真实测试的目录。产物名称以 ``traffic_``
   开头，因此不会匹配 doctest 默认的 ``test*.txt`` glob。

已知限制
-----------------

* 直接使用 httpx 的 ``iter_raw()`` 或 ``requests`` 的 ``response.raw`` 会绕过收集。
* aiohttp 的请求体在写入点（``ClientRequest.write_bytes``）取样，得到的是实际发出的
  载荷字节，multipart 的 boundary 也是真的。只有当请求没走到写入点（例如连接在
  发送前就失败）或传输层会压缩上传体时，才按 ``json=``/``data=`` 推导，并在 HAR 中
  标记 ``_body_synthesized``。带该标记的体不保证等于线上字节——会话级的
  ``json_serialize`` 等设置会改变实际序列化结果，因此不应作为证据使用。上传被
  提前响应或连接中断打断时，只记录已经发出的前缀：标 ``_truncated`` 和
  ``_body_synthesized``，``bodySize`` 为 ``-1``（真实长度未知）。
* 显式传入 ``allow_redirects=False`` 的嵌套请求与重定向跳转无法区分，不会单独记录。
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
   数据。请勿将 ``captured_traffic/`` 提交到版本控制。在多用户主机上，请限制对捕获
   目录的访问。

开发
-----------

运行测试套件::

    pip install tox
    tox

许可证
-------

基于 MIT 许可证条款分发。