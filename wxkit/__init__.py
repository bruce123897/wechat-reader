"""wxkit：本机微信 4.x 聊天记录的只读工具（后台同步 + 整理好的档案库 + wx 命令）。

模块分工（每个文件只做一件事，改哪块就看哪块）：
  config    路径、账号、数据目录、配置文件
  crypto    SQLCipher 4 页解密、密钥校验
  keys      密钥的保存、读取、导入
  mirror    把加密库增量解成明文镜像（只补变化的页和 WAL）
  contacts  联系人、群、群昵称
  parse     单条原始消息 → 整理好的一行（类型、正文、附加信息）
  archive   档案库（只增不删，撤回原文也留着）
  ingest    镜像 → 档案库的增量入库
  voice     语音转文字（后台子进程）
  media     图片 / 视频 / 文件定位与解密
  imgkey    V2 图片密钥自动获取与样本验证
  timeutil  时间解析与格式化（北京时间）
  render    紧凑输出与输出上限
  query     给命令和脚本用的查询接口
  daemon    后台常驻循环
  cli       wx 命令入口
"""
__version__ = '2.3.1'
__author__ = 'Bruce'

NAME = 'WeChat for AI'
CONTACT = '微信 bruce_jaychou'
SIGN = f'{NAME} v{__version__} · 维护者 {__author__} · {CONTACT}'
PITCH = '装不上、用不了、想要新功能，或者想聊聊怎么让 AI 帮你处理微信和生意，都可以加我'

