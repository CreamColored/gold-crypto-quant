"""项目启动入口。

PyCharm 直接运行本文件即可查看项目状态；也可以在运行参数中加入
``db-check`` 或 ``init-db`` 执行数据库检查与初始化。
"""

from gold_crypto_quant.cli import main

if __name__ == "__main__":
    # 只有直接运行 main.py 时才调用命令行入口；被测试或其他模块导入时不会自动执行。
    main()
