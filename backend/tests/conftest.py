import os
import tempfile

# 必须在测试模块导入 app 之前设置，避免测试任务写入用户的正式本地数据库。
os.environ["VTW_DATA_DIR"] = tempfile.mkdtemp(prefix="vtw-tests-")
os.environ["VTW_WORKER_ENABLED"] = "false"
