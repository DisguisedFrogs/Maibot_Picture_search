"""抓取/下载异常层级：连接层可切代理（CandidateFailure），站点拒绝与超限不可切。"""

class FetchError(Exception):
    """抓取/下载失败的基类。"""


class CandidateFailure(FetchError):
    """连接层失败（拒连/超时/总超时）——可切换代理候选重试。"""


class SiteError(FetchError):
    """站点拒绝（HTTP 4xx/5xx）——属网站问题，不切换代理。"""


class SizeLimitError(FetchError):
    """响应体超限——已停止下载，不切换代理。"""

