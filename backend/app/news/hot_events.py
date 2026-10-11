"""把一个交易日的资讯收成具体事件，而不是宽行业榜。

上一收盘（15:00）之后到下一交易日 9:25 的资讯归入下一交易日，和隔夜竞价是同一窗口。
周末打开页面时，这场开盘已经有稿就列出这些还在等待竞价的事件；还没有就显示上一个交易日。

聚类看标题里的主体、动作，以及标题里出现的细概念，再加同一时间窗。正文里抽到、
但标题没写的概念不参与聚类，避免旁支概念把无关消息粘成一件事。同一主体和动作，
或标题高度相似，会在较短时间窗内合并。「A与B签署」和「B与A签署」是同一组双方，
不分前后；双方和动作都相同的，合并窗口是整个交易日（上一收盘到下一 9:25），
不再卡 6 小时。摘要只在标题没有主体时参与，避免各条资讯共用的页脚把整天粘成一件事。
宽行业名不参与聚类，也不拿去扩成分股。

展示标题由模型一次写完要展示的事件，最多 20 条，按成员标题指纹缓存，缓存版本仍是 v10。
模型要给出完整中文标题，尽量不超过 28 字并写完整，写明主体、动作和宾语，不带时间戳、括号和省略号。
规则只做校验：不合格就记下原因再问一次，仍不行就退回清理后的原标题，再不行就丢掉。
清理会去掉开头的钟点、【】标签、末尾省略号、已知媒体来源前缀（央视财经、财联社、新华社），券商「电新/计算机/研究称」，以及研报文件名里的日期戳和 #，并停在句号前。
公司名后面的冒号不是来源。后面如果以「公司」或「本公司」起笔，就换成这家公司的名字。
栏目标题、只有来源名或公司名、把「前法官」写成「在前法官」的标题，以及只有点评、重点、没有动作的稿，不进列表。
侧面佐证、印证、看好、地位、逻辑、关注、把握这类没有主体和动作的点评不是事件。持续关注开头，以及 1）、① 这类列表残句，也不是事件。
国务院、证监会、工信部、发改委、国资委、央行或点名的公司后面，布局、建议、推荐是动作，不是点评。
增长、新增、追加这类业绩和回购动作算事件，但同比、数据显示这种没有具体主体的不算。
已经点名的公司不能换成「公司」，这个对照看原始标题，不看清掉冒号之后的句子。叠加、接续粘成的长标题收成不超过 20 字的核心事件。
签约、签署、战略合作要留下双方，不能只剩建发股份或只剩优必选。
动词贴在句首、后面又接上另一组主体和动作的，是语序颠倒，退回清理后的原标题。
截断必须停在分句边界，不能把「集体涨价」切成「集体」。
没超过 28 字不截短，不切开连续的英文字母或数字，也不能把还带宾语的句子收成只有动作的半截。
这种半截标题不能再单独占一条，免得和完整事件重复。
标题末尾的券商组名（天风计算机）去掉。观点更新、话题用 & 拼起来的研报标签、
停在「近八」这种没写完的数量，以及只有产品名没有主体的「Cybercab扩容提速」，都不进列表。
奖项、文体、体育这类和资本市场无关、又没有 A 股映射的事件不进列表。

概念先留标题里已经出现的细概念，再用主体到 A 股概念的小词典补回被滤空的映射。
概念必须对得上标题里的主体：伊朗、中东映射到中东，不能因为都出现了「冲突」就标成俄乌。
「俄乌冲突快报」这类栏目名不是概念。事件自己没有概念时，模型只能从这张词典里选。词典没写到的无关概念仍然丢掉。
同一时间窗里，财政部、国务院这类国内政策主体即使一个写发布、一个写出台，也并成一件事。
"""
from __future__ import annotations

import copy
import hashlib
import logging
import math
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from datetime import time as dt_time

from app.market_time import CN_TZ, cn_now, current_trading_day
from app.news.collectors import is_red, parse_time
from app.news.extract import _usable_sector_name
from app.news.store import NewsStore

logger = logging.getLogger(__name__)

_LOOKBACK_DAYS = 21
_CACHE_SECONDS = 600
_CLUSTER_WINDOW = timedelta(hours=6)
_HEAT_HALF_LIFE_HOURS = 12
# 加红稿在热度里按 2.5 条计，普通稿仍按 1 条。
_RED_MENTION_WEIGHT = 2.5
# 要展示的事件一次批量起标题，不再逐条各问一次。
_LLM_TITLE_BATCH = 20
_LLM_TITLE_TOKENS = 1600
_TITLE_LIMIT = 20
# 展示用的分句可以比模型标题稍长，避免为了卡 20 字把宾语、数字或动词从中间切开。
_TITLE_HARD = 28
# 模型标题只拒绝真正的残句。28 字是提示里的目标，不是验收上限。
_LLM_TITLE_MAX = 48
# 清理后的回退标题如果停在这些残词上，不能拿来顶替没通过的模型标题。
_FALLBACK_BAD_TAILS = ("集体", "进行", "中", "资", "的", "和", "与")
_FALLBACK_BAD_OPENINGS = ("市场没有", "我们认为", "点评")
_CJK_SLASH = re.compile(r"[\u4e00-\u9fff]\s*[/／]\s*[\u4e00-\u9fff]")
# 标题缓存按这个版本取。截断、交易日合并和研报残句的判定变了，旧键不再套用。
_TITLE_CACHE_VERSION = 10
_CONCEPT_LIMIT = 4

# 申万一级和口头上的大板块。出现在标题里也不够把两条资讯收成同一事件。
BROAD_SECTORS = frozenset({
    "人工智能", "机器人", "半导体", "新能源", "医药", "银行", "白酒", "军工",
    "消费", "地产", "房地产", "券商", "证券", "有色", "煤炭", "钢铁", "电力",
    "汽车", "通信", "计算机", "电子", "传媒", "农业", "化工", "机械", "建筑",
    "交运", "家电", "商贸", "纺织", "轻工", "综合", "公用事业", "非银金融",
    "国防军工", "美容护理", "社会服务", "环保", "石油石化", "基础化工",
    "有色金属", "交通运输", "建筑材料", "建筑装饰", "商贸零售", "纺织服饰",
    "轻工制造", "电力设备", "机械设备", "农林牧渔", "医药生物", "家用电器",
    "食品饮料",
})
_ACTIONS = (
    "发布", "出台", "定调", "涨价", "降价", "提价", "上调", "下调", "报价", "收购", "中标", "获批", "上市",
    "回购", "签约", "签署", "签订", "停产", "召回", "立案", "补贴", "降准", "降息", "加息", "制裁", "收紧",
    "追加", "订单", "增长", "新增", "加速释放",
)
# 先看更具体的市场语境，再退回动作本身。六个分类覆盖政策、海外、产业、地缘、商品和公司。
CATEGORY_NAMES = (
    "国内政策/宏观",
    "海外市场/央行",
    "科技与产业",
    "地缘政治",
    "大宗商品/期货价格异动",
    "公司重大事项",
)
_CATEGORY_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("地缘政治", ("制裁", "关税", "冲突", "战争", "停火", "出口管制", "配额", "地缘")),
    ("海外市场/央行", ("美联储", "欧央行", "日本央行", "英央行", "加息", "降息", "美股", "纳指", "标普", "欧股", "日经")),
    ("国内政策/宏观", ("出台", "补贴", "降准", "国务院", "发改委", "工信部", "证监会", "财政部", "国资委", "央行", "宏观", "政策")),
    ("大宗商品/期货价格异动", ("碳酸锂", "原油", "期货", "黄金", "铜价", "铁矿", "煤炭", "稀土", "豆粕", "螺纹", "报价", "橡胶")),
    ("公司重大事项", ("收购", "回购", "上市", "立案", "停产", "召回", "签约", "签署", "签订", "中标", "停牌", "退市")),
    ("科技与产业", ("发布", "订单", "追加", "获批", "涨价", "降价", "提价", "上调", "下调", "量产", "芯片", "模型")),
)
_ACTION_CATEGORY = {
    "出台": "国内政策/宏观",
    "定调": "国内政策/宏观",
    "补贴": "国内政策/宏观",
    "降准": "国内政策/宏观",
    "加息": "海外市场/央行",
    "降息": "海外市场/央行",
    "制裁": "地缘政治",
    "收紧": "地缘政治",
    "报价": "大宗商品/期货价格异动",
    "收购": "公司重大事项",
    "回购": "公司重大事项",
    "上市": "公司重大事项",
    "立案": "公司重大事项",
    "停产": "公司重大事项",
    "召回": "公司重大事项",
    "签约": "公司重大事项",
    "中标": "公司重大事项",
    "增长": "公司重大事项",
    "新增": "公司重大事项",
}
_BEAR_CUES = ("制裁", "立案", "停产", "召回", "下调", "降价", "收紧", "冲突", "战争", "关税", "暴跌", "下跌", "加息")
_BULL_CUES = ("补贴", "获批", "订单", "涨价", "回购", "发布", "出台", "降息", "降准", "上调", "提价", "中标", "签约", "上涨")
_NOISE_EXACT = frozenset({"图片", "广告", "视频", "转发", "分享", "推广", "赞助"})
_NOISE_HINTS = ("闲聊", "广告", "推广", "赞助", "加微信", "点击领取", "转发微博")
_RELEVANCE_MIN = 3
# 重要性高于映射、新鲜度和热度。热度封顶，避免条数把寻常涨价顶过降息或政策。
# 盘面验证另计，权重和重要性同级，不放进这个封顶。
_IMPORTANCE_POINTS = {"琐碎": 0, "一般": 20, "重要": 55, "重大": 130}
_IMPORTANCE_ORDER = ("琐碎", "一般", "重要", "重大")
_MAPPING_CONCEPT = 15
_MAPPING_ASSET = 15
_MAPPING_DIRECTION = 5
_FRESH_MAX = 20
_HEAT_BONUS_CAP = 15
_MAJOR_RATE = ("降息", "加息", "降准", "美联储", "欧央行", "日本央行", "英央行")
_MAJOR_ORGS = ("国务院", "发改委", "工信部", "证监会", "财政部", "央行")
_MAJOR_POLICY_ACTS = ("出台", "补贴", "政策", "降准")
_MAJOR_GEO = ("制裁", "战争", "关税", "出口管制", "配额")
_MAJOR_COMMODITIES = ("碳酸锂", "原油", "黄金", "铜价", "铁矿")
_PRICE_VERBS = ("报价", "涨价", "降价", "提价", "上调", "下调", "上涨", "下跌", "暴涨", "暴跌")
_LEADERS = (
    "华为", "宁德时代", "贵州茅台", "英伟达", "苹果", "特斯拉", "比亚迪",
    "平安银行", "腾讯", "阿里巴巴", "茅台",
)
# 标题里点名的机构。缺主体时从代表标题里补这些，而不是留下「可能还会」「运行情况」。
_ORG_SUBJECTS = (
    "美联储", "欧洲央行", "欧央行", "日本央行", "英格兰银行", "英央行",
    "国务院", "政治局", "发改委", "工信部", "证监会", "财政部", "国资委",
    "商务部", "外交部", "住建部", "国家能源局", "能源局", "国常会",
    "央行",
)
_EXPLICIT_SUBJECTS = tuple(sorted(set(_ORG_SUBJECTS) | set(_LEADERS), key=len, reverse=True))
# 没有具体对象的词。不能单靠它们把两条资讯收成一件事。
_GENERIC_TOKENS = frozenset({
    "超预期", "不及预期", "预期", "公告", "消息", "行情", "价格", "股价",
    "政策", "市场", "公司", "股份", "板块", "概念", "产业链", "产业",
    "上涨", "下跌", "大涨", "大跌",
})
# 概念名和标题只共用这些词时，不能当成同一件事。冲突两个字会把伊朗稿贴上俄乌。
_CONCEPT_GLUE = frozenset({
    "冲突", "战争", "危机", "局势", "问题", "方面", "情况", "事件",
    "最新", "焦点", "热点", "快报", "解决", "构想", "消息",
})
_SUBJECT_TRAIL = (
    "价格", "股价", "期货", "行情", "板块", "概念", "产业链", "产业",
    "公司", "集团", "股份", "厂商", "企业",
)
# 这些词当主语时，标题还缺一个能指认的主体。
_VAGUE_CUES = (
    "可能", "或许", "预计", "有望", "或将", "料将",
    "还会", "还将", "几次", "多次",
    "情况", "谋划", "宏观",
    "进一步", "消息称", "据悉", "传闻称",
)
_SIMILAR_WINDOW = timedelta(hours=3)
_SIMILARITY_MIN = 0.72
_REPRESENTATIVE_SIMILARITY = 0.45
_LEADER_ACTS = ("发布", "收购", "回购", "停产", "制裁", "订单", "上市", "立案")
_IMPORTANT_ACTS = ("涨价", "获批", "回购", "订单", "收购", "中标")
_IMPORTANT_CATEGORIES = frozenset({
    "国内政策/宏观", "海外市场/央行", "地缘政治", "公司重大事项",
})
_LEAD_SUFFIXES = ("宣布", "称", "表示", "指出", "消息", "传闻", "公司", "披露")
_STOP = frozenset({
    "公司", "市场", "今日", "表示", "消息", "记者", "财经", "股份", "有限",
    "集团", "板块", "概念", "龙头", "资金", "大涨", "大跌", "上涨", "下跌",
    "走强", "走弱", "午后", "盘中", "持续", "继续", "相关", "中国", "美国",
    "国内", "海外", "全球", "多家", "有关", "A股", "港股",
})
_CLAUSE = re.compile(r"[，。！？、；：:\n]")
# 半句话常见的起笔。完整标题不会从这些词开始。
_FRAGMENT_LEADS = (
    "再叠加", "叠加", "再加上", "加上",
    "和之前", "和此前", "和这次", "和本次", "和上述", "以及",
    "根据", "据悉", "据称", "据报道",
    "与此同时", "与此",
    "同时", "此外", "另外", "并且", "而且",
    "但是", "不过", "然而", "虽然", "尽管", "如果", "因为", "由于", "随着",
    "其中", "对此", "为此", "因此", "因而",
    "继而", "随后", "此后", "日前", "此前", "还有",
)
_FRAGMENT_PREFIX = re.compile("^(?:" + "|".join(_FRAGMENT_LEADS) + ")")
_TIME_PREFIX = re.compile(r"^(?:昨夜|昨晚|今日|昨日|今天|昨天|日前|此前|本次|这次|当前|目前|最近|刚刚)+")
# 涨跌叙述不是聚类动作，但标题被从中间截开时，它往往才是这件事的谓语。
_PRICE_NARRATIVE = (
    "大涨", "大跌", "上涨", "下跌", "暴涨", "暴跌",
    "走强", "走弱", "走高", "走低", "飙升", "跳水",
)
# 聚类停用词里的国别可以当标题主体。这里只挡「公司」「市场」这种没有对象的词。
_NOT_SUBJECT = frozenset({
    "公司", "市场", "今日", "表示", "消息", "记者", "财经", "股份", "有限",
    "集团", "板块", "概念", "龙头", "资金", "多家", "有关", "相关",
    "持续", "继续", "午后", "盘中",
})
# 叙述谓语后面再接这些动作，就是两件不相干的事被粘在了一起。
_GLUE_FOLLOWUPS = frozenset({
    "发布", "出台", "收购", "中标", "获批", "上市", "回购", "签约",
    "停产", "召回", "立案", "补贴", "制裁", "收紧", "降息", "加息", "降准",
})
_DANGLING_FINAL = frozenset("的了和与及等把被将已正而或但之以为于对从在超")
# 以这些字结尾时，只有最后两个字是完整词才算没截断。
_CLOSED_TAILS = {
    "政": frozenset({"新政", "政府", "财政", "行政", "市政", "内政", "党政", "军政"}),
}
_SOURCE_HEADS = (
    "财联社", "华尔街见闻", "证券时报", "上海证券报", "中国证券报",
    "第一财经", "澎湃新闻", "新浪财经", "新华社", "界面新闻",
    "央视财经", "格隆汇", "金十数据", "金十",
    "路透社", "路透", "彭博社", "彭博", "南华早报",
)
_SOURCE_NAMES = frozenset(_SOURCE_HEADS)
_SOURCE_ALT = "|".join(sorted(_SOURCE_NAMES, key=len, reverse=True))
_SOURCE_BRACKET = re.compile(r"^(?:【[^】]{1,16}】|\[[^\]]{1,16}\]|（[^）]{1,12}）|\([^)]{1,12}\))")
# 残缺的前导标签，例如「新增隐债】」。成对的【】在后面整段去掉。
_BROKEN_BRACKET = re.compile(r"^[^【】\[\]]{1,16}[】\]]")
_BRACKET_PAIR = re.compile(r"【[^】]*】|\[[^\]]*\]")
_STRAY_BRACKET = re.compile(r"[【】\[\]]")
# 双字动词被从中间切开时，标题会停在第一个字上，例如「出台」变成「出」。
_VERB_PAIRS = ("出台", "发布", "上调", "下调", "降息", "加息", "推出", "宣布")
# 国内政策机构。同一窗口里动词不同（发布/出台）仍视为同一件事。央行利率不在这里。
_POLICY_ORGS = (
    "财政部", "国务院", "政治局", "发改委", "工信部", "证监会",
    "商务部", "央行", "国常会",
)
# 标题里点了主体、但提及被相关性滤空时，用这张表补 A 股概念。键必须同时出现。
_SUBJECT_CONCEPTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("橡胶",), ("橡胶", "天然橡胶")),
    (("原油",), ("石油", "油服")),
    (("IEA",), ("石油", "油服")),
    (("美联储", "加息"), ("黄金", "银行")),
    (("伊朗",), ("中东",)),
    (("以色列",), ("中东",)),
    (("加沙",), ("中东",)),
    (("巴勒斯坦",), ("中东",)),
    (("中东",), ("中东",)),
)
# 概念里点名的地缘实体必须在事件正文里出现。同一组里任一说法都算对上。
_GEO_ENTITY_GROUPS = (
    ("俄乌", "乌克兰"),
    ("伊朗", "中东", "以色列", "加沙", "巴勒斯坦"),
    ("俄罗斯",),
)
# 没有 A 股映射时直接丢掉。发布、获奖这类弱动词留不住诺贝尔和世界杯。
_HARD_NON_MARKET = (
    "诺贝尔", "奥斯卡", "金球奖", "格莱美", "金像奖", "金马奖", "艾美奖", "普利策", "图灵奖",
    "世界杯", "欧冠", "奥运会", "冬奥会", "亚运会", "欧洲杯", "亚洲杯",
    "英超", "西甲", "德甲", "意甲", "法甲", "中超", "NBA", "CBA",
)
# 政策、价格里顺带提到演出时仍保留。纯文体稿没有这些市场动作就丢掉。
_SOFT_NON_MARKET = ("演唱会", "电影节", "新专辑")
_STRONG_MARKET = (
    "出台", "补贴", "降准", "降息", "加息", "涨价", "降价", "提价", "上调", "下调",
    "回购", "收购", "立案", "制裁", "定调", "政策",
    *_PRICE_NARRATIVE,
)
_SOURCE_DATELINE = re.compile(
    r"^(?:" + _SOURCE_ALT + r")(?:\d{1,2}月\d{1,2}日)?(?:电|讯)[，,]?"
)
_SOURCE_LABEL = re.compile(
    r"^(?:(?:" + _SOURCE_ALT + r")(?:报道|快讯|讯|电)?|快讯|突发|独家|刚刚)[：:|｜]"
)
# 招商电新称、中信计算机称、华泰研究称。监管机构研究称不是券商前缀。
_BROKER_NOTE = re.compile(
    r"^(?!(?:国务院|发改委|工信部|证监会|财政部|央行|国资委|商务部|美联储|政治局|欧央行))"
    r"[\u4e00-\u9fff]{2,8}(?:电新|计算机|研究)称"
)
# 末尾或冒号前的组名。天风计算机、招商电新。没有这些券商名时，不因为句子里有「计算机」就删。
_BROKER_HOUSES = (
    "天风", "招商", "中信", "华泰", "中金", "国泰君安", "海通", "广发", "兴业",
    "申万", "国信", "安信", "方正", "长江", "东吴", "浙商", "中泰", "国金", "光大",
)
_BROKER_DESKS = ("计算机", "电新", "研究", "策略")
_BROKER_HOUSE = "|".join(sorted(_BROKER_HOUSES, key=len, reverse=True))
_BROKER_DESK = "|".join(_BROKER_DESKS)
_BROKER_TEAM_LEAD = re.compile(rf"^(?:{_BROKER_HOUSE})(?:{_BROKER_DESK})[：:]")
_BROKER_TEAM_TAIL = re.compile(rf"(?:{_BROKER_HOUSE})(?:{_BROKER_DESK})$")
# PCB设备&钓针观点更新这类研报标签。普通标题里的斜杠另有规则。
_TOPIC_JOIN = re.compile(r"[\u4e00-\u9fff][&＆][\u4e00-\u9fff]")
# 停在「近八」而后面没有单位，是半截数量，不是「近八成」或「降息25基点」。
_MID_PHRASE_CUT = re.compile(r"(?:近|约|逾)[0-9０-９一二三四五六七八九十]+$")
# 只有拉丁产品名加扩容、提速，没有中文主体。
_SUBJECTLESS_PRODUCT = re.compile(r"^[A-Za-z][A-Za-z0-9]+(?:扩容|提速|加速|放量|提产)+$")
# 1）和①是列表残句，不是事件标题。
_LIST_MARKER = re.compile(r"(?:[0-9]+[)）]|[①②③④⑤⑥⑦⑧⑨⑩⑪⑫])")
_REUTERS_TAIL = re.compile(r"(?:[-|｜]\s*)?(?:Reuters|路透社|路透)$")
# 15:32【财联社】被冒号切开后，会剩「32【…】」这种钟点碎片。
_CLOCK_PREFIX = re.compile(r"^(?:\d{1,2}[:：]\d{2})+")
_MINUTE_BEFORE_BRACKET = re.compile(r"^\d{1,2}(?=【)")
_LEFTOVER_MINUTE = re.compile(r"^\d{1,2}(?![年月日号点时分])(?=[\u4e00-\u9fff])")
_TRAILING_ELLIPSIS = re.compile(r"(?:\.{2,}|…+|。{2,})+$")
_SENTENCE_END = re.compile(r"[。；！？\n]")
# 机器翻译把 former judge 写成「在前法官」。只拦紧挨着职务的「在前」，
# 「在前三季度」「在前述」是正常说法。
_FORMER_OFFICES = (
    "副董事长", "副总统", "副总理", "副部长", "董事长", "发言人",
    "总统", "首相", "总理", "主席", "财长", "外长", "防长", "行长",
    "部长", "法官", "局长", "省长", "市长", "州长", "议长", "院长",
    "署长", "书记", "总裁", "董事", "首席", "大使", "国王", "女王", "元首",
    "CEO", "CFO", "CTO", "COO",
)
_BROKEN_FORMER = re.compile(
    r"在前(?:" + "|".join(_FORMER_OFFICES) + r")",
    re.IGNORECASE,
)
_FILE_EXT = re.compile(r"(?i)\.(?:pdf|docx?|xlsx?|pptx?|txt|csv)$")
_COMPACT_DATE = r"20\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])"
_DASHED_DATE = r"20\d{2}[-./](?:0?[1-9]|1[0-2])[-./](?:0?[1-9]|[12]\d|3[01])"
# 文件名日期：前面是连字符，或后面紧跟 #。正文里的「央行2026-10-09」不在这里。
_FILE_DATE = re.compile(
    rf"(?:(?<=[-_—–])(?:{_COMPACT_DATE}|{_DASHED_DATE})(?!\d)"
    rf"|(?<!\d)(?:{_COMPACT_DATE}|{_DASHED_DATE})(?=[#＃]))"
)
_REPORT_KINDS = ("更新", "报告", "研报", "点评", "周报", "日报", "月报", "纪要", "简报")
_REPORT_PREFIX = re.compile(
    r"^(?:[\u4e00-\u9fffA-Za-z0-9]{1,24}(?:" + "|".join(_REPORT_KINDS) + r"))"
    r"[-_—–]+"
    r"(?:20\d{6}|20\d{2}[-./](?:0?[1-9]|1[0-2])[-./](?:0?[1-9]|[12]\d|3[01]))"
    r"[-_—–]*"
)
_SECTION_MATERIAL = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,12}材料$")
# 价格叙述后面再出现这些动作，就是下一句被粘进了标题。
_UNGLUE_VERBS = _GLUE_FOLLOWUPS | frozenset({"涨价", "降价", "提价"})
# 没有具体主体和动作时，这些是点评，不是事件。
_COMMENTARY_CUES = (
    "侧面佐证", "印证", "利好", "看好", "地位", "逻辑",
    "关注", "建议", "推荐", "布局", "把握",
)
# 机构或公司名后面，这三个词是政策或公司动作，不是点评。
_DIRECTIVE_VERBS = ("布局", "建议", "推荐")
_VAGUE_ACTORS = frozenset({
    "机构", "研报", "分析", "分析师", "券商", "行业", "板块", "市场",
    "资金", "投资者", "业内", "人士", "专家", "报告", "点评", "策略",
    "继续", "建议", "推荐", "布局", "关注", "把握",
})
# 模型若把「立讯精密」写成「公司」或「本公司」，标题不能用。
_GENERIC_SUBJECT_HEADS = ("本公司", "公司", "企业", "厂商", "集团")
# 叠加、接续把原因和事件粘成一句时，只留后面的核心事件。
_RUNON_GLUE = ("再叠加", "叠加", "接续", "再加上", "加上", "以及", "并且", "同时")
_CAUSE_TAILS = ("需求", "压力", "影响", "带动", "推动", "背景", "因素")
# 增长前面只有这些词时，还没有具体主体。
_STAT_PREFIXES = ("数据显示", "统计显示", "据统计", "同比", "环比")

_CACHE: dict[tuple, dict] = {}
_LLM_CACHE: dict[str, dict] = {}


def clear_hot_event_cache(*, llm: bool = True) -> None:
    _CACHE.clear()
    if llm:
        _LLM_CACHE.clear()


def build_top_hot_events(now: datetime | None, store: NewsStore) -> dict:
    now = (now or cn_now()).astimezone(CN_TZ)
    key = (str(store.path), int(now.timestamp()) // _CACHE_SECONDS)
    cached = _CACHE.get(key)
    if cached is not None:
        return copy.deepcopy(cached)
    snapshot = _compute(now, store)
    _CACHE[key] = snapshot
    return copy.deepcopy(snapshot)


def _active_session_day(now: datetime) -> date:
    """当前这份榜对应的交易日。周末是下一场开盘，工作日是当天。"""
    day = now.astimezone(CN_TZ).date()
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _news_session_day(published: datetime) -> date:
    """资讯归入哪一个交易日的热门榜。和竞价确认窗口同一口径。"""
    from app.news.market_confirm import _confirm_day

    return _confirm_day(published)


def _auction_still_ahead(now: datetime, chosen: date) -> bool:
    local = now.astimezone(CN_TZ)
    return local < datetime.combine(chosen, dt_time(9, 25), CN_TZ)


def _compute(now: datetime, store: NewsStore) -> dict:
    today = now.date()
    trading = current_trading_day(now)
    active = _active_session_day(now)
    start = datetime.combine(today - timedelta(days=_LOOKBACK_DAYS), dt_time.min, CN_TZ)
    end = datetime.combine(today + timedelta(days=1), dt_time.min, CN_TZ)
    by_day: dict[date, list[dict]] = defaultdict(list)
    for row in store.items_between(start, end):
        published = parse_time(row.get("published_at"))
        if published is None:
            continue
        published = published.astimezone(CN_TZ)
        # 还没发布的稿不进榜，避免周末提前看到当天更晚的快讯。
        if published > now:
            continue
        row = dict(row)
        row["published"] = published
        by_day[_news_session_day(published)].append(row)
    ranked = {day: _cluster_day(rows, now) for day, rows in by_day.items()}
    ranked = {day: events for day, events in ranked.items() if events}
    fallback = False
    if active in ranked:
        chosen: date | None = active
    elif trading in ranked:
        # 周末还没有隔夜稿时，继续显示上一个交易日，不算回退。
        chosen = trading
    else:
        earlier = [day for day in ranked if day < active]
        chosen = max(earlier) if earlier else None
        fallback = chosen is not None
    if chosen is None:
        return {
            "as_of": None,
            "trading_day": trading.isoformat(),
            "fallback": False,
            "hint": None,
            "updated_at": None,
            "events": [],
        }
    events = ranked[chosen]
    from app.news.market_confirm import attach_confirmations
    attach_confirmations(events, now)
    events = _rank_events(events, now, grade=False)
    _label_with_llm(events)
    for event in events:
        _ensure_shown_name(event)
    events = [event for event in events if _keep_shown_event(event)]
    events = _drop_cutoff_duplicates(events)
    if not events:
        return {
            "as_of": None,
            "trading_day": trading.isoformat(),
            "fallback": False,
            "hint": None,
            "updated_at": None,
            "events": [],
        }
    events = _rank_events(events, now, grade=False)
    latest = max(event.pop("_latest") for event in events)
    for event in events:
        event.pop("_fp", None)
        event.pop("_first", None)
        event.pop("_batch_id", None)
        event.pop("_llm_title_applied", None)
    updated_hm = latest.astimezone(CN_TZ).strftime("%H:%M")
    return {
        "as_of": chosen.isoformat(),
        "trading_day": (chosen if not fallback else trading).isoformat(),
        "fallback": fallback,
        "hint": _hint(now, trading, chosen, fallback, updated_hm),
        "updated_at": updated_hm,
        "events": events,
    }


def _cluster_day(rows: list[dict], now: datetime) -> list[dict]:
    docs = [_doc(row) for row in rows]
    docs = [doc for doc in docs if doc is not None]
    if not docs:
        return []
    parent = list(range(len(docs)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for left in range(len(docs)):
        for right in range(left + 1, len(docs)):
            if _can_merge(docs[left], docs[right]):
                parent[find(right)] = find(left)
    groups: dict[int, list[dict]] = defaultdict(list)
    for index, doc in enumerate(docs):
        groups[find(index)].append(doc)
    events = []
    for members in groups.values():
        event = _event_from(members, now)
        # 加红稿即使关键词标题还不完整也先留下，后面还有模型和回退标题。
        if _cleaned_display_ok(event.get("name")) or int(event.get("red_count") or 0) > 0:
            events.append(event)
    return _rank_events(events, now, grade=True)


def _doc(row: dict) -> dict | None:
    title = str(row.get("title") or "").strip()
    summary = _summary_text(row)
    if not title and not summary:
        return None
    source = str(row.get("source") or "")
    red = is_red(source, row.get("level"))
    published = row["published"]
    concepts: list[str] = []
    stocks: dict[str, str] = {}
    for mention in row.get("mentions") or []:
        kind = mention["kind"]
        key = str(mention["key"] or "").strip()
        name = str(mention["name"] or key).strip()
        if not key:
            continue
        if kind == "sector" and _fine_concept(key, name):
            concepts.append(name or key)
        elif kind == "stock":
            stocks[key] = name or key
    lead, action, obj = "", "", ""
    phrase = _strip_report_label(title)
    if phrase and phrase != _compact(title):
        lead, action, obj = _parse_phrase(phrase)
    if not lead or not action:
        lead, action, obj = _parse_phrase(title)
    if not lead or not action:
        lead, action, obj = _parse_phrase(summary)
    shown = title or summary[:80]
    relevance = _relevance(shown, lead, action, concepts, stocks)
    # 加红不依赖涨价、发布这类动词。普通稿仍要主体和动作都在，才不会把闲聊收进榜。
    if relevance < _RELEVANCE_MIN and not red:
        return None
    entity_text = title
    if summary and not _explicit_entities(title):
        entity_text = f"{title}\n{summary[:160]}"
    entities = _explicit_entities(title)
    if not entities and _subject_is_vague(lead):
        entities = _explicit_entities(summary[:200])
    tokens = _title_tokens(lead, obj, concepts, title or summary[:80], entities)
    return {
        "item_id": row.get("id"),
        "source": source,
        "red": red,
        "published": published,
        "title": title or summary[:80],
        "entity_text": entity_text,
        "text": f"{title}\n{summary}",
        "lead": lead,
        "action": action,
        "object": obj,
        "entities": entities,
        "relevance": relevance,
        "concepts": concepts,
        "stocks": stocks,
        "tokens": tokens,
    }


def _summary_text(row: dict) -> str:
    raw = row.get("extra_json") or ""
    if raw:
        try:
            import json
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = {}
        if isinstance(payload, dict):
            zh = str(payload.get("summary_zh") or "").strip()
            if zh:
                return zh
    return str(row.get("clean_text") or "").strip()


def _is_column_concept(name: str) -> bool:
    """来源栏目不是 A 股概念。俄乌冲突快报只是专栏名。"""
    return "快报" in _compact(name)


def _fine_concept(key: str, name: str) -> bool:
    text = (name or key).strip()
    if _is_column_concept(text) or _is_column_concept(key):
        return False
    if text in BROAD_SECTORS or key in BROAD_SECTORS:
        return False
    return _usable_sector_name(text)


def _parse_phrase(text: str) -> tuple[str, str, str]:
    body = (text or "").strip()
    if not body:
        return "", "", ""
    clauses = [clause.strip() for clause in _CLAUSE.split(body) if clause.strip()]
    chosen = body
    action = ""
    for clause in clauses or [body]:
        for verb in _ACTIONS:
            if verb in clause:
                chosen = clause
                action = verb
                break
        if action:
            break
    if not action:
        for clause in clauses or [body]:
            index, verb = _directive_action(clause)
            if index >= 0 and verb:
                chosen = clause
                action = verb
                break
    if not action:
        for clause in clauses or [body]:
            index, verb = _narrative_action(clause)
            if verb and _subject_ok(clause[:index]):
                chosen = clause
                action = verb
                break
    if not action:
        return _clean_lead(chosen[:8]), "", ""
    index = chosen.find(action)
    lead = _clean_lead(chosen[:index])
    obj = _CLAUSE.split(chosen[index + len(action):])[0].strip()[:8]
    if len(obj) < 2 or obj in _STOP or obj in BROAD_SECTORS or obj in _GENERIC_TOKENS:
        obj = ""
    return lead, action, obj


def _clean_lead(text: str) -> str:
    lead = (text or "").strip()
    for suffix in _LEAD_SUFFIXES:
        if lead.endswith(suffix) and len(lead) - len(suffix) >= 2:
            lead = lead[:-len(suffix)]
    if len(lead) < 2 or lead in _STOP or lead in BROAD_SECTORS:
        return ""
    return lead[:8]


def _title_tokens(
    lead: str,
    obj: str,
    concepts: list[str],
    title: str,
    entities: list[str] | None = None,
) -> set[str]:
    """聚类词只来自标题里的主体、宾语和标题中出现的细概念。

    正文提及的概念不进聚类词，否则一条快讯带上的苹果、黄金会把橡胶新闻粘进去。
    """
    tokens = set()
    for value in (lead, obj, _subject_core(lead)):
        if (
            value
            and len(value) >= 2
            and value not in _STOP
            and value not in BROAD_SECTORS
            and value not in _GENERIC_TOKENS
        ):
            tokens.add(value)
    for entity in entities or []:
        if entity:
            tokens.add(entity)
    for concept in concepts:
        if concept and concept in (title or "") and concept not in BROAD_SECTORS:
            tokens.add(concept)
    return tokens


def _event_from(members: list[dict], now: datetime) -> dict:
    members = sorted(members, key=lambda item: item["published"])
    first = members[0]["published"]
    latest = members[-1]["published"]
    sources = {item["source"] for item in members if item["source"]}
    age_hours = max(0.0, (now - latest).total_seconds() / 3600)
    decay = math.exp(-math.log(2) * age_hours / _HEAT_HALF_LIFE_HOURS)
    mentions = len(members)
    red_count = sum(1 for item in members if item.get("red"))
    weighted_mentions = (mentions - red_count) + red_count * _RED_MENTION_WEIGHT
    source_count = len(sources) or 1
    name = _keyword_title(members)
    focused = _representative_members(members, name)
    concept_counts: Counter[str] = Counter()
    for item in focused:
        concept_counts.update(dict.fromkeys(item["concepts"], 1))
    focus_titles = [str(item.get("title") or "") for item in focused]
    subjects = _subject_set(name, focused)
    concepts = _merge_mapped_concepts(
        _relevant_concepts(concept_counts, focus_titles, subjects),
        "\n".join([name, *focus_titles]),
    )
    rejected = [label for label in concept_counts if label not in concepts and label not in BROAD_SECTORS]
    stocks: dict[str, dict] = {}
    for item in focused:
        for key, stock_name in item["stocks"].items():
            if not _stock_relevant(stock_name, key, focus_titles, subjects, concepts, rejected):
                continue
            hit = stocks.get(key)
            if hit is None:
                hit = {"key": key, "name": stock_name, "mentions": 0, "sources": set()}
                stocks[key] = hit
            hit["mentions"] += 1
            hit["sources"].add(item["source"])
            hit["name"] = stock_name
    mentioned = [
        {
            "key": hit["key"],
            "name": hit["name"],
            "mentions": hit["mentions"],
            "source_count": len(hit["sources"]),
        }
        for hit in stocks.values()
    ]
    mentioned.sort(key=lambda item: (-item["mentions"], -item["source_count"], item["name"]))
    blob = "\n".join(focus_titles)
    action = _mode([item["action"] for item in focused if item["action"]])
    category = _category_for(blob, action)
    direction = _direction_for(blob, action)
    for hit in mentioned:
        hit["direction"] = direction
    item_ids = [str(item["item_id"]) for item in members]
    digest = hashlib.sha1(",".join(sorted(item_ids)).encode("utf-8")).hexdigest()[:12]
    seen = first.astimezone(CN_TZ)
    first_seen = seen.strftime("%H:%M") if seen.date() == now.date() else seen.strftime("%m-%d %H:%M")
    headlines: list[str] = []
    for item in members:
        title = str(item["title"] or "").strip()
        if title and title not in headlines:
            headlines.append(title)
        if len(headlines) >= 3:
            break
    headline = headlines[0] if headlines else ""
    return {
        "key": f"ev_{digest}",
        "name": name,
        "category": category,
        "direction": direction,
        "relevance": max(int(item.get("relevance") or 0) for item in members),
        "concepts": concepts,
        "mapping": [{"name": concept, "kind": "concept", "direction": direction} for concept in concepts],
        "headline": headline,
        "headlines": headlines,
        "mentions": mentions,
        "red_count": red_count,
        "source_count": len(sources),
        "first_seen": first_seen,
        "updated_at": first_seen,
        "heat": round(weighted_mentions * source_count * decay, 4),
        "mentioned_stocks": mentioned,
        "item_ids": item_ids,
        "_latest": latest,
        "_first": first,
        "_fp": hashlib.sha1("\n".join(item["title"] for item in members).encode("utf-8")).hexdigest(),
    }


def _rank_events(events: list[dict], now: datetime, *, grade: bool) -> list[dict]:
    """按重要性、映射、新鲜度、热度排序。只有第一次按规则分级时丢掉琐碎小事。"""
    kept = []
    for event in events:
        if grade:
            event["importance"] = _rule_importance(event)
        _score_event(event, now)
        if grade and _drop_minor(event):
            continue
        kept.append(event)
    kept.sort(key=lambda item: (
        -float(item["score"]),
        -_IMPORTANCE_POINTS.get(str(item.get("importance")), 0),
        -float(item["breakdown"]["mapping"]),
        -float(item["breakdown"]["freshness"]),
        -float(item["heat"]),
        str(item["name"]),
    ))
    return kept


def _score_event(event: dict, now: datetime) -> None:
    label = str(event.get("importance") or "一般")
    if label not in _IMPORTANCE_POINTS:
        label = "一般"
        event["importance"] = label
    importance = _IMPORTANCE_POINTS[label]
    concepts = [str(name).strip() for name in (event.get("concepts") or []) if str(name).strip()]
    stocks = [item for item in (event.get("mentioned_stocks") or []) if isinstance(item, dict)]
    mapping = 0
    if concepts:
        mapping += _MAPPING_CONCEPT
    if stocks:
        mapping += _MAPPING_ASSET
    direction = str(event.get("direction") or "")
    if direction in {"利好", "利空"} and (concepts or stocks):
        mapping += _MAPPING_DIRECTION
    first = event.get("_first")
    if isinstance(first, datetime):
        age_hours = max(0.0, (now - first).total_seconds() / 3600)
    else:
        age_hours = _HEAT_HALF_LIFE_HOURS
    freshness = _FRESH_MAX * math.exp(-math.log(2) * age_hours / _HEAT_HALF_LIFE_HOURS)
    try:
        heat = float(event.get("heat") or 0)
    except (TypeError, ValueError):
        heat = 0.0
    heat_bonus = min(_HEAT_BONUS_CAP, max(0.0, heat))
    confirmation = _confirmation_points(event.get("confirmation"))
    event["breakdown"] = {
        "importance": importance,
        "confirmation": confirmation,
        "mapping": mapping,
        "freshness": round(freshness, 4),
        "heat": round(heat_bonus, 4),
    }
    event["score"] = round(importance + confirmation + mapping + freshness + heat_bonus, 4)


def _confirmation_points(raw) -> float:
    if not isinstance(raw, dict):
        return 0.0
    try:
        points = float(raw.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0
    if points != points:
        return 0.0
    return round(min(80.0, max(0.0, points)), 4)


def _event_text(event: dict) -> str:
    parts = [str(event.get("name") or ""), str(event.get("headline") or "")]
    parts.extend(str(item) for item in (event.get("headlines") or []))
    return "\n".join(part for part in parts if part)


def _rule_importance(event: dict) -> str:
    text = _event_text(event)
    if _is_major(text):
        label = "重大"
    elif _is_important(text, str(event.get("category") or "")):
        label = "重要"
    else:
        label = "一般"
    if "传闻" in text:
        label = _IMPORTANCE_ORDER[max(0, _IMPORTANCE_ORDER.index(label) - 1)]
    return label


def _is_major(text: str) -> bool:
    if any(cue in text for cue in _MAJOR_RATE):
        return True
    if any(org in text for org in _MAJOR_ORGS) and any(act in text for act in _MAJOR_POLICY_ACTS):
        return True
    if any(cue in text for cue in _MAJOR_GEO):
        return True
    if any(name in text for name in _MAJOR_COMMODITIES) and any(verb in text for verb in _PRICE_VERBS):
        return True
    return any(name in text for name in _LEADERS) and any(act in text for act in _LEADER_ACTS)


def _is_important(text: str, category: str) -> bool:
    if any(act in text for act in _IMPORTANT_ACTS):
        return True
    if category in _IMPORTANT_CATEGORIES:
        return True
    return any(name in text for name in _LEADERS) and any(verb in text for verb in _PRICE_VERBS)


def _drop_minor(event: dict) -> bool:
    """没有映射、只有一条来源的寻常小事不进榜。重大和重要事件留下。"""
    label = str(event.get("importance") or "")
    if label == "琐碎":
        return True
    if label != "一般":
        return False
    if float(event["breakdown"]["mapping"]) > 0:
        return False
    if int(event.get("red_count") or 0) > 0:
        return False
    return int(event.get("mentions") or 0) <= 1 and int(event.get("source_count") or 0) <= 1


def _specific_concepts(counts: Counter[str]) -> list[str]:
    names = [name for name in counts if name not in BROAD_SECTORS]
    names.sort(key=len, reverse=True)
    kept: list[str] = []
    for name in names:
        if any(name != other and name in other for other in kept):
            continue
        kept.append(name)
    kept.sort(key=lambda name: (-counts[name], -len(name), name))
    return kept[:_CONCEPT_LIMIT]


def _display_name_ok(name: object) -> bool:
    text = _compact(str(name or ""))
    return bool(text) and text != "事件"


def _subject_core(lead: str) -> str:
    core = _compact(lead)
    original = core
    changed = True
    while changed and len(core) > 2:
        changed = False
        for suffix in _SUBJECT_TRAIL:
            if core.endswith(suffix) and len(core) - len(suffix) >= 2:
                core = core[:-len(suffix)]
                changed = True
                break
    if not core or core == original or len(core) < 2:
        return ""
    if core in _STOP or core in _GENERIC_TOKENS or core in BROAD_SECTORS:
        return ""
    return core


def _explicit_entities(text: str) -> list[str]:
    if not text:
        return []
    occupied = [False] * len(text)
    spans: list[tuple[int, str]] = []
    for name in _EXPLICIT_SUBJECTS:
        start = 0
        while True:
            index = text.find(name, start)
            if index < 0:
                break
            end = index + len(name)
            if not any(occupied[index:end]):
                spans.append((index, name))
                for pos in range(index, end):
                    occupied[pos] = True
            start = end
    spans.sort()
    seen: list[str] = []
    for _index, name in spans:
        if name not in seen:
            seen.append(name)
    return seen


def _subject_is_vague(lead: str) -> bool:
    text = _normalize_subject(lead) or _subject_text(lead)
    if len(text) < 2:
        return True
    if _explicit_entities(text):
        return False
    if text in _NOT_SUBJECT or text in BROAD_SECTORS or text in _STOP:
        return True
    if text.endswith(("性", "的", "地")):
        return True
    return any(cue in text for cue in _VAGUE_CUES)


def _norm_title(text: str) -> str:
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", _strip_source_prefix(text))


def _headline_similarity(left: str, right: str) -> float:
    aa = _norm_title(left)
    bb = _norm_title(right)
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0

    def grams(text: str) -> set[str]:
        if len(text) < 2:
            return {text}
        return {text[i:i + 2] for i in range(len(text) - 1)}

    ga, gb = grams(aa), grams(bb)
    union = len(ga | gb)
    if not union:
        return 0.0
    return len(ga & gb) / union


def _subject_keys(doc: dict) -> set[str]:
    keys = {entity for entity in (doc.get("entities") or []) if entity}
    lead = str(doc.get("lead") or "")
    if lead and not _subject_is_vague(lead):
        if len(lead) <= 8:
            keys.add(lead)
        core = _subject_core(lead)
        if core:
            keys.add(core)
    return keys


def _same_subject_action(left: dict, right: dict) -> bool:
    if not left.get("action") or left.get("action") != right.get("action"):
        return False
    return bool(_subject_keys(left) & _subject_keys(right))


_PARTY_PAIR = re.compile(
    r"([\u4e00-\u9fffA-Za-z0-9]{2,12})[与和及]([\u4e00-\u9fffA-Za-z0-9]{2,12})"
)


def _party_pair(text: str) -> tuple[str, str]:
    """「赛力斯与宁德时代签署」里的双方。动作紧跟在双方后面才算。"""
    body = _compact(_strip_source_prefix(text))
    body = re.sub(r"([\u4e00-\u9fffA-Za-z0-9]{2,12})[：:]与", r"\1与", body)
    index, verb = _tuple_action(body)
    if index < 0 or not verb:
        return "", ""
    head = _TIME_PREFIX.sub("", body[:index])
    match = _PARTY_PAIR.fullmatch(head)
    if not match:
        return "", ""
    left, right = match.group(1), match.group(2)
    blocked = _NOT_SUBJECT | _STOP | _SOURCE_NAMES | _GENERIC_TOKENS
    if left in blocked or right in blocked or left == right:
        return "", ""
    return left, right


def _same_party_action(left: dict, right: dict) -> bool:
    """A与B做同一件事，和 B与A 是同一事件。动作不同则不并。"""
    if not left.get("action") or left.get("action") != right.get("action"):
        return False
    first = _party_pair(str(left.get("title") or ""))
    second = _party_pair(str(right.get("title") or ""))
    if not first[0] or not second[0]:
        return False
    return frozenset(first) == frozenset(second)


def _policy_subject(doc: dict) -> str:
    """财政部增量政策和隐债方案是同一件事，即使一条写发布、另一条写出台。"""
    blob = "\n".join([
        str(doc.get("title") or ""),
        str(doc.get("lead") or ""),
        " ".join(str(entity) for entity in (doc.get("entities") or [])),
    ])
    for org in _POLICY_ORGS:
        if org in blob:
            return org
    if "财政" in blob and any(cue in blob for cue in ("增量", "隐债", "化债", "出台", "政策")):
        return "财政部"
    return ""


def _same_policy_subject(left: dict, right: dict) -> bool:
    subject = _policy_subject(left)
    return bool(subject) and subject == _policy_subject(right)


def _can_merge(left: dict, right: dict) -> bool:
    # 同一组双方做同一件事时，合并这一交易日里的全部稿，00:13 和 08:39 也算一件。
    # 其他合并仍看 6 小时。聚类按交易日分桶，不会把上一场收盘后的稿并进上午。
    if _same_party_action(left, right):
        return True
    gap = abs(left["published"] - right["published"])
    if gap > _CLUSTER_WINDOW:
        return False
    if _same_subject_action(left, right):
        return True
    if _same_policy_subject(left, right):
        return True
    if (
        gap <= _SIMILAR_WINDOW
        and _headline_similarity(str(left.get("title") or ""), str(right.get("title") or "")) >= _SIMILARITY_MIN
    ):
        return True
    shared = set(left.get("tokens") or ()) & set(right.get("tokens") or ())
    shared = {token for token in shared if len(token) >= 3 and token not in _GENERIC_TOKENS}
    if not shared:
        return False
    return not (left.get("action") and right.get("action") and left.get("action") != right.get("action"))


def _subject_set(name: str, members: list[dict]) -> set[str]:
    subjects = set(_explicit_entities(name))
    lead, _action, _obj = _split_headline(name)
    if lead and not _subject_is_vague(lead):
        if len(lead) <= 8:
            subjects.add(lead)
        core = _subject_core(lead)
        if core:
            subjects.add(core)
    for item in members:
        for entity in item.get("entities") or []:
            if entity:
                subjects.add(entity)
        item_lead = str(item.get("lead") or "")
        if item_lead and not _subject_is_vague(item_lead):
            if len(item_lead) <= 8:
                subjects.add(item_lead)
            core = _subject_core(item_lead)
            if core:
                subjects.add(core)
    return {subject for subject in subjects if subject and len(subject) >= 2}


def _matches_event(item: dict, name: str, subjects: set[str]) -> bool:
    title = str(item.get("title") or "")
    if not title:
        return False
    if any(len(subject) >= 2 and subject in title for subject in subjects):
        return True
    if name and _headline_similarity(title, name) >= _REPRESENTATIVE_SIMILARITY:
        return True
    norm_name = _norm_title(name)
    for token in item.get("tokens") or ():
        if len(str(token)) >= 3 and str(token) in norm_name:
            return True
    return False


def _representative_members(members: list[dict], name: str) -> list[dict]:
    subjects = _subject_set(name, [])
    chosen = [item for item in members if _matches_event(item, name, subjects)]
    return chosen or list(members)


def _concept_entity_miss(name: str, blob: str) -> bool:
    """概念点了俄乌，正文却只有伊朗，就不能留。同一组里出现任一说法算对上。"""
    for group in _GEO_ENTITY_GROUPS:
        if not any(token in name for token in group):
            continue
        if any(token in blob for token in group):
            continue
        return True
    return False


def _concept_relevant(name: str, blob: str, subjects: set[str]) -> bool:
    if _is_column_concept(name) or _concept_entity_miss(name, blob):
        return False
    if name and name in blob:
        return True
    # 细概念延伸标题里已经出现的词，例如标题写「机器人」、提及是「工业机器人」。
    if _headline_maps_concept(name, blob):
        return True
    checked: list[str] = []
    for subject in subjects:
        checked.append(subject)
        core = _subject_core(subject)
        if core:
            checked.append(core)
    for token in checked:
        if len(token) < 2 or token in _GENERIC_TOKENS or token in _STOP or token in _CONCEPT_GLUE:
            continue
        if token in name:
            return True
        if len(token) <= 8 and name in token:
            return True
    return False


def _headline_maps_concept(name: str, blob: str) -> bool:
    if len(name) < 3 or not blob or name in blob:
        return False
    for size in range(min(len(name) - 1, 6), 1, -1):
        for start in range(0, len(name) - size + 1):
            token = name[start:start + size]
            if token in _GENERIC_TOKENS or token in _STOP or token in _CONCEPT_GLUE:
                continue
            if token in blob:
                return True
    return False


def _relevant_concepts(counts: Counter[str], titles: list[str], subjects: set[str]) -> list[str]:
    blob = "\n".join(titles)
    filtered: Counter[str] = Counter({
        name: count
        for name, count in counts.items()
        if _concept_relevant(str(name), blob, subjects)
    })
    return _specific_concepts(filtered)


def _dictionary_names() -> tuple[str, ...]:
    names: list[str] = []
    for _keys, concepts in _SUBJECT_CONCEPTS:
        for name in concepts:
            if name not in names:
                names.append(name)
    for name in ("基建", "地方债"):
        if name not in names:
            names.append(name)
    return tuple(names)


def _dictionary_concepts(blob: str) -> list[str]:
    """主体在标题里、提及却是空的时候，补上对应的 A 股概念。"""
    found: list[str] = []

    def add(names: tuple[str, ...]) -> None:
        for name in names:
            if name not in found:
                found.append(name)

    for keys, concepts in _SUBJECT_CONCEPTS:
        if all(key in blob for key in keys):
            add(concepts)
    fiscal = "财政部" in blob or (
        "财政" in blob and any(cue in blob for cue in ("增量", "隐债", "化债", "出台", "政策"))
    )
    if fiscal and any(cue in blob for cue in ("增量", "隐债", "化债", "出台", "政策")):
        add(("基建", "地方债"))
    return found


def _merge_mapped_concepts(concepts: list[str], blob: str) -> list[str]:
    merged: list[str] = []
    for name in [*_dictionary_concepts(blob), *concepts]:
        if name and not _is_column_concept(name) and name not in merged:
            merged.append(name)
        if len(merged) >= _CONCEPT_LIMIT:
            break
    return merged


def _stock_relevant(
    name: str,
    key: str,
    titles: list[str],
    subjects: set[str],
    kept_concepts: list[str],
    rejected: list[str],
) -> bool:
    """代表成员上的个股默认留下。名字只贴着被丢掉的概念时才剔除。"""
    label = str(name or "").strip()
    blob = "\n".join(titles)
    if label and label in blob:
        return True
    if key and str(key) in blob:
        return True
    tokens: list[str] = list(kept_concepts)
    for subject in subjects:
        tokens.append(subject)
        core = _subject_core(subject)
        if core:
            tokens.append(core)
    if label and any(len(token) >= 2 and token not in _GENERIC_TOKENS and token in label for token in tokens):
        return True
    tied_to_rejected = bool(label) and any(
        len(concept) >= 2 and (concept in label or label in concept) for concept in rejected
    )
    return not tied_to_rejected


def _ends_split_verb(text: str) -> bool:
    if not text or any(text.endswith(verb) for verb in _VERB_PAIRS):
        return False
    return any(text.endswith(verb[0]) for verb in _VERB_PAIRS)


def _ends_mid_number(text: str) -> bool:
    return bool(text) and text[-1].isdigit()


def _verb_spans(text: str) -> list[tuple[int, str]]:
    """按出现位置列出动词。同一处只留更长的那个。"""
    found: list[tuple[int, str]] = []
    for verb in (*_ACTIONS, *_PRICE_NARRATIVE, *_DIRECTIVE_VERBS, "重申"):
        start = 0
        while True:
            index = text.find(verb, start)
            if index < 0:
                break
            found.append((index, verb))
            start = index + len(verb)
    found.sort(key=lambda item: (item[0], -len(item[1])))
    kept: list[tuple[int, str]] = []
    occupied = -1
    for index, verb in found:
        if index < occupied:
            continue
        kept.append((index, verb))
        occupied = index + len(verb)
    return kept


def _misordered_title(text: str) -> bool:
    """句首动词没有主体，后面又接上另一组主体和动作。重申…白宫…签署就是这种。"""
    body = re.sub(r"[《》「」]", "", _compact(text))
    spans = _verb_spans(body)
    if len(spans) < 2:
        return False
    index, verb = spans[0]
    if index > 0 and _subject_ok(body[:index]):
        return False
    for later, _later_verb in spans[1:]:
        lead = body[index + len(verb):later]
        if _subject_ok(lead):
            return True
    return False


def _watch_or_list_fragment(text: str) -> bool:
    """持续关注开头，或带 1）、① 的列表残句，不是事件。"""
    body = _compact(text)
    if not body:
        return False
    if body.startswith("持续关注"):
        return True
    return _LIST_MARKER.search(body) is not None


def _display_clause_ok(text: str) -> bool:
    """整句可以比 20 字长，但不能停在半个动词、半个数字或半个词上。"""
    body = _compact(text)
    if (
        _title_incoherent(body)
        or _has_file_debris(body)
        or _watch_or_list_fragment(body)
        or _misordered_title(body)
        or _surface_block_reason(body)
    ):
        return False
    if not body or body == "事件" or len(body) < 4 or len(body) > _TITLE_HARD:
        return False
    if any(char in body for char in "【】[]"):
        return False
    if _starts_fragment(body) or _ends_abruptly(body) or _ends_split_verb(body) or _ends_mid_number(body):
        return False
    lead, action, obj = _split_headline(body)
    if not action or not _subject_ok(lead):
        return False
    consumed = len(lead) + len(action) + len(obj)
    return not body[consumed:].strip("，。！？、；：")


def _colon_subject(head: str) -> bool:
    """冒号左边是公司或机构简称，不是已经剥过的媒体名。"""
    if not head or len(head) > 8 or head in _SOURCE_NAMES:
        return False
    if _tuple_action(head)[0] >= 0 or _narrative_action(head)[0] >= 0:
        return False
    if head in _NOT_SUBJECT or head in _STOP or head in BROAD_SECTORS or head in _GENERIC_TOKENS:
        return False
    return bool(re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9]{2,8}", head))


def _join_colon_head(head: str, nxt: str) -> str:
    """公司名不是来源。后面以公司/本公司起笔就换上这家公司，与/和则把双方接上。"""
    if not _colon_subject(head) or not nxt:
        return ""
    for generic in ("本公司", "公司"):
        if nxt.startswith(generic):
            return head + nxt[len(generic):]
    if nxt.startswith("与") and not nxt.startswith("与此"):
        return head + nxt
    if nxt.startswith("和") and not _starts_fragment(nxt):
        return head + nxt
    if _starts_fragment(nxt):
        return ""
    if _tuple_action(nxt)[0] == 0 or _narrative_action(nxt)[0] == 0:
        return ""
    if _tuple_action(nxt)[0] > 0 or _narrative_action(nxt)[0] > 0:
        return head + nxt
    return ""


def _stitch_subject_verb(body: str) -> str:
    """「IEA：下调原油供需预期」的冒号把主体和动词拆开了，先接回去。

    已知媒体前缀已经剥过。公司名后面的冒号同样接回去，不能把公司名当成来源丢掉。
    """
    parts = [part.strip() for part in _CLAUSE.split(body) if part.strip()]
    if len(parts) < 2:
        return body
    head, nxt = parts[0], parts[1]
    joined = _join_colon_head(head, nxt)
    if not joined:
        if len(head) > 8 or _tuple_action(head)[0] >= 0 or _narrative_action(head)[0] >= 0:
            return body
        if _tuple_action(nxt)[0] != 0 and _narrative_action(nxt)[0] != 0:
            return body
        joined = head + nxt
    parts[0] = joined
    del parts[1]
    return parts[0] if len(parts) == 1 else parts[0] + "，" + "，".join(parts[1:])


def _cut_ellipsis_tail(text: str) -> str:
    """省略号后面的「持续关注」是点评尾巴，事件停在省略号前。"""
    match = re.search(r"\.{2,}|…+", text or "")
    if not match:
        return text
    head = text[:match.start()].strip("，。！？、；：")
    return head or text


def _prepare_headline(text: str) -> str:
    body = _cut_ellipsis_tail(_strip_source_prefix(text))
    body = _first_sentence(_stitch_subject_verb(body))
    return _attach_following_object(_cut_glued_sentence(body))


def _cleaned_headline(text: str) -> str:
    """去掉来源前缀和钟点，停在第一句，过长时再停在分句内部的自然断点。"""
    body = _prepare_headline(text)
    if not body or body == "事件":
        return ""
    signed = _signing_title(body)
    parts = [part.strip() for part in _CLAUSE.split(body) if part.strip()] or [body]
    for part in parts:
        cleaned = _TIME_PREFIX.sub("", _drop_leading_fragment(part)).strip("，。！？、；：\"'“”")
        if cleaned and cleaned != "事件":
            clipped = _clip_at_boundary(cleaned)
            if clipped and clipped != "事件":
                return _prefer_signed(clipped, body, signed)
    return signed


def _core_event_clause(text: str) -> str:
    """超过 20 字、又用叠加或接续粘了原因时，只留后面的核心事件。"""
    body = _compact(text)
    if len(body) <= _TITLE_LIMIT:
        return body
    cut_at = -1
    span = 0
    for glue in _RUNON_GLUE:
        index = body.rfind(glue)
        if index > cut_at:
            cut_at = index
            span = len(glue)
    if cut_at < 0:
        return body
    core = body[cut_at + span:].lstrip("，。！？、；：")
    if len(core) > _TITLE_LIMIT:
        for tail in _CAUSE_TAILS:
            index = core.find(tail)
            if not 0 < index <= 8:
                continue
            rest = core[index + len(tail):].lstrip("的")
            if 6 <= len(rest) <= _TITLE_LIMIT:
                core = rest
                break
    if core and core != body and 6 <= len(core) <= _TITLE_LIMIT and not _starts_fragment(core):
        return core
    return body


def _is_ascii_alnum(char: str) -> bool:
    return bool(char) and char.isascii() and char.isalnum()


def _retreat_open_alnum(text: str, window: str) -> str:
    """截断点落在字母或数字中间时，整串退回，不留下 Vicor对V 这种半个词。"""
    if not window or not text.startswith(window) or len(window) >= len(text):
        return window
    if not (_is_ascii_alnum(window[-1]) and _is_ascii_alnum(text[len(window)])):
        return window
    while window and _is_ascii_alnum(window[-1]):
        window = window[:-1]
    return window


def _clip_at_boundary(text: str) -> str:
    if (
        _title_incoherent(text)
        or _watch_or_list_fragment(text)
        or _misordered_title(text)
        or _surface_block_reason(text)
    ):
        return ""
    core = _core_event_clause(text)
    if core and core != _compact(text):
        text = core
    if _has_file_debris(text):
        text = _strip_report_label(text)
    if not text or text == "事件" or _surface_block_reason(text):
        return ""
    if _display_clause_ok(text):
        return text
    signed = _signing_title(text)
    if signed:
        return signed
    # 没超过上限就整句留下。残词（停在「政」上）仍要削掉，那不是按字数截断。
    # 超长才按上限截，并且不能切开连续的字母或数字。
    if len(text) <= _TITLE_HARD:
        window = text
        if _ends_abruptly(window) or _ends_split_verb(window) or _ends_mid_number(window):
            while window and (
                _ends_abruptly(window)
                or _ends_split_verb(window)
                or _ends_mid_number(window)
                or window[-1] in "的和与及"
            ):
                window = window[:-1]
    else:
        window = _retreat_open_alnum(text, text[:_TITLE_HARD])
        if _ends_mid_number(window):
            while window and window[-1].isdigit():
                window = window[:-1]
            window = _retreat_open_alnum(text, window)
        if len(text) > len(window) and _ends_split_verb(window):
            window = window[:-1]
        cut = -1
        for sep in ("以及", "并且", "同时", "和", "与", "及"):
            index = window.rfind(sep)
            if index >= 4:
                cut = max(cut, index)
        if cut >= 4:
            window = window[:cut]
        while window and (
            _ends_abruptly(window)
            or _ends_split_verb(window)
            or _ends_mid_number(window)
            or window[-1] in "的和与及"
        ):
            window = window[:-1]
        window = _retreat_open_alnum(text, window)
    if _display_clause_ok(window) and not _drops_known_action(window, text):
        return window
    repaired = _repair_headline(text)
    if (
        repaired
        and not _drops_known_action(repaired, text)
        and not _is_harmful_cutoff(repaired, text)
        and (_display_clause_ok(repaired) or _title_ok(repaired))
    ):
        return repaired
    if (
        len(window) >= 4
        and window != "事件"
        and not _is_section_label(window)
        and not _ends_abruptly(window)
        and not _ends_split_verb(window)
        and not _ends_mid_number(window)
        and not _surface_block_reason(window)
        and not _drops_known_action(window, text)
    ):
        return window
    return ""


def _ensure_subject(title: str, source: str) -> str:
    text = _compact(title)
    if not _display_name_ok(text):
        return ""
    lead, action, _obj = _split_headline(text)
    if action and lead and not _subject_is_vague(lead):
        return text
    entities = _explicit_entities(source)
    if not entities:
        return text
    if lead and any(entity in lead for entity in entities) and not _subject_is_vague(lead):
        return text
    attached = _attach_entity(text, entities[0])
    return attached if _display_name_ok(attached) else text


def _attach_entity(title: str, entity: str) -> str:
    if not entity:
        return title
    lead, action, obj = _split_headline(title)
    if entity in title and lead and not _subject_is_vague(lead):
        return title
    prefixed = title if entity in title else f"{entity}{title}"
    prefixed_lead, _prefixed_action, _prefixed_obj = _split_headline(prefixed)
    if (
        prefixed_lead
        and not _subject_is_vague(prefixed_lead)
        and (_display_clause_ok(prefixed) or (len(prefixed) <= _TITLE_LIMIT and _title_ok(prefixed)))
    ):
        return prefixed
    if action:
        fitted = _fit_object(f"{entity}{action}", obj)
        if _display_clause_ok(fitted) or _title_ok(fitted):
            return fitted
    if len(prefixed) <= _TITLE_LIMIT and _title_ok(prefixed):
        return prefixed
    return title


def _keyword_title(members: list[dict]) -> str:
    """用代表标题的第一分句，不再把各条的主体、动作、宾语拼在一起。

    合格分句优先。没有合格分句时，退回去掉来源前缀、按分句截断的原标题。
    仍然没有可读文本时返回空字符串，调用方把这条事件丢掉，不再显示「事件」。
    """
    clauses: list[str] = []
    for item in members:
        clause = _representative_clause(str(item.get("title") or ""))
        if clause and clause != "事件":
            clauses.append(clause)
    blob = "\n".join(
        str(item.get("entity_text") or item.get("title") or "")
        for item in members
    )
    if clauses:
        counts = Counter(clauses)
        best = max(counts.values())
        for clause in clauses:
            if counts[clause] != best:
                continue
            ensured = _ensure_subject(clause, blob)
            if _cleaned_display_ok(ensured):
                return ensured
    for item in members:
        cleaned = _cleaned_headline(str(item.get("title") or ""))
        if not cleaned:
            continue
        ensured = _ensure_subject(cleaned, blob)
        if _cleaned_display_ok(ensured):
            return ensured
    return ""


def _is_noise(title: str) -> bool:
    text = re.sub(r"\s+", "", (title or "").strip())
    if not text or text in _NOISE_EXACT or len(text) < 4:
        return True
    if sum(1 for char in text if "\u4e00" <= char <= "\u9fff") < 2:
        return True
    return any(hint in text for hint in _NOISE_HINTS)


def _relevance(title: str, lead: str, action: str, concepts: list[str], stocks: dict) -> int:
    """有主体也有动作才算一条能进榜的资讯。图片、闲聊和广告是 0。"""
    if _is_noise(title) or not lead or not action:
        return 0
    score = 3
    if concepts:
        score += 1
    if stocks:
        score += 1
    return score


def _category_for(text: str, action: str) -> str:
    blob = text or ""
    for name, cues in _CATEGORY_CUES:
        if any(cue in blob for cue in cues):
            return name
    return _ACTION_CATEGORY.get(action, "科技与产业")


def _direction_for(text: str, action: str) -> str:
    blob = text or ""
    if any(cue in blob for cue in _BEAR_CUES):
        return "利空"
    if any(cue in blob for cue in _BULL_CUES):
        return "利好"
    if action in {"降价", "下调", "立案", "停产", "召回", "制裁", "收紧", "加息"}:
        return "利空"
    return "利好"


def _mode(values: list[str]) -> str:
    if not values:
        return ""
    return Counter(values).most_common(1)[0][0]


def _compact(text: str) -> str:
    body = re.sub(r"\s+", "", (text or "").strip())
    return body.strip("，。！？、；：\"'“”")


def _title_incoherent(text: str) -> bool:
    """「在前」紧挨职务才是拆词，例如「在前法官」。在前三季度、在前述不是。"""
    return bool(_BROKEN_FORMER.search(_compact(text)))


def _file_debris_reason(text: str) -> str:
    body = _compact(text)
    if "#" in body or "＃" in body or _FILE_EXT.search(body):
        return "含文件标记"
    if _FILE_DATE.search(body):
        return "含日期戳"
    return ""


def _has_file_debris(text: str) -> bool:
    return bool(_file_debris_reason(text))


def _surface_block_reason(text: str) -> str:
    """研报标签、没写完的数量、没有主体的产品名、整句只剩券商组名。"""
    body = _compact(text)
    if not body:
        return ""
    if "观点更新" in body or _TOPIC_JOIN.search(body):
        return "研报标签"
    if _MID_PHRASE_CUT.search(body):
        return "结尾不完整"
    if _SUBJECTLESS_PRODUCT.fullmatch(body):
        return "缺少主体"
    if _BROKER_TEAM_TAIL.search(body):
        return "券商组名"
    return ""


def _looks_like_report_file(text: str) -> bool:
    body = _FILE_EXT.sub("", _compact(text))
    if not body:
        return False
    if _FILE_DATE.search(body):
        return True
    return any(body.endswith(kind) for kind in _REPORT_KINDS)


def _strip_report_label(text: str) -> str:
    """去掉研报文件名。只删连字符后或 # 前的日期，正文里的央行日期留下。"""
    body = _FILE_EXT.sub("", _compact(text))
    parts = re.split(r"[#＃]", body)
    if len(parts) >= 2 and _looks_like_report_file(parts[0]):
        body = _compact("".join(parts[1:]))
    elif len(parts) >= 2:
        body = _compact("".join(parts))
    body = _REPORT_PREFIX.sub("", body)
    body = _FILE_DATE.sub("", body)
    return body.strip("-_—–#＃")


def _strip_source_prefix(text: str) -> str:
    body = _compact(text)
    body = _CLOCK_PREFIX.sub("", body)
    body = _MINUTE_BEFORE_BRACKET.sub("", body)
    changed = True
    while body and changed:
        changed = False
        for pattern in (_SOURCE_BRACKET, _SOURCE_DATELINE, _SOURCE_LABEL, _BROKER_NOTE, _BROKER_TEAM_LEAD):
            updated = pattern.sub("", body, count=1)
            if updated != body:
                body = updated.lstrip("，。！？、；：")
                changed = True
    body = _BROKER_TEAM_TAIL.sub("", body)
    body = _REUTERS_TAIL.sub("", body)
    body = _BROKEN_BRACKET.sub("", body)
    body = _BRACKET_PAIR.sub("", body)
    body = _STRAY_BRACKET.sub("", body)
    body = _LEFTOVER_MINUTE.sub("", body)
    body = _TRAILING_ELLIPSIS.sub("", body)
    body = _strip_report_label(body)
    return body.strip("，。！？、；：")


def _first_sentence(text: str) -> str:
    body = (text or "").strip()
    if not body:
        return ""
    parts = [part.strip() for part in _SENTENCE_END.split(body) if part.strip()]
    return parts[0] if parts else body


def _starts_fragment(text: str) -> bool:
    if not text:
        return False
    if _FRAGMENT_PREFIX.match(text):
        return True
    return text.startswith("与")


def _drop_leading_fragment(text: str) -> str:
    body = text
    while body:
        match = _FRAGMENT_PREFIX.match(body)
        if match:
            body = body[match.end():]
            continue
        if body.startswith("与"):
            body = body[1:]
            continue
        break
    return body


def _ends_abruptly(text: str) -> bool:
    if not text:
        return True
    last = text[-1]
    if last in _DANGLING_FINAL:
        return True
    allowed = _CLOSED_TAILS.get(last)
    return allowed is not None and text[-2:] not in allowed


def _subject_text(before: str) -> str:
    lead = _compact(before)
    changed = True
    while changed and lead:
        changed = False
        for suffix in _LEAD_SUFFIXES:
            if lead.endswith(suffix) and len(lead) - len(suffix) >= 2:
                lead = lead[:-len(suffix)]
                changed = True
                break
    return lead


def _subject_ok(before: str) -> bool:
    lead = _subject_text(before)
    if len(lead) < 2 or lead in _NOT_SUBJECT or lead in BROAD_SECTORS:
        return False
    if _starts_fragment(lead) or lead.endswith(("性", "的", "地", "超")):
        return False
    if lead.endswith(("针对", "对于", "关于")):
        return False
    if any(word in lead for word in _PRICE_NARRATIVE):
        return False
    return _tuple_action(lead)[0] < 0


def _concrete_subject(before: str) -> bool:
    """同比、数据显示不是主体。国别和公司名可以。"""
    text = _subject_text(before)
    if len(text) < 2 or text in _NOT_SUBJECT or text in _GENERIC_TOKENS:
        return False
    core = text
    for prefix in _STAT_PREFIXES:
        if core.startswith(prefix):
            core = core[len(prefix):].lstrip("的")
            break
    if len(core) < 2 or core in {"显示", "表明", "称"}:
        return False
    return core not in _NOT_SUBJECT and core not in _GENERIC_TOKENS


def _normalize_subject(before: str) -> str:
    lead = _drop_leading_fragment(_subject_text(before))
    lead = _TIME_PREFIX.sub("", lead)
    for suffix in ("针对", "对于", "关于"):
        if lead.endswith(suffix) and len(lead) - len(suffix) >= 2:
            lead = lead[:-len(suffix)]
    return lead


def _qualified_directive_subject(before: str) -> bool:
    """布局、建议、推荐只有跟在监管机构或公司名后面才算动作。"""
    text = _subject_text(before)
    if not text or not _subject_ok(text):
        return False
    if any(org in text for org in _ORG_SUBJECTS) or any(name in text for name in _LEADERS):
        return True
    if text in BROAD_SECTORS or any(word in text for word in _VAGUE_ACTORS):
        return False
    if text in _NOT_SUBJECT or text in _GENERIC_TOKENS:
        return False
    return len(text) >= 3 and bool(re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9]+", text))


def _directive_action(text: str) -> tuple[int, str]:
    """机构或公司名后面的布局、建议、推荐。句首的建议关注、推荐布局不是。"""
    found: tuple[int, str] | None = None
    for verb in _DIRECTIVE_VERBS:
        start = 0
        while True:
            index = text.find(verb, start)
            if index < 0:
                break
            if index >= 2 and _qualified_directive_subject(text[:index]):
                if found is None or index < found[0] or (index == found[0] and len(verb) > len(found[1])):
                    found = (index, verb)
                break
            start = index + len(verb)
    return found if found else (-1, "")


def _tuple_action(text: str) -> tuple[int, str]:
    """取最靠前的动作。追加回购要落在追加上，宾语才是回购，而不是空的。"""
    found: tuple[int, str] | None = None
    for verb in _ACTIONS:
        index = text.find(verb)
        if index < 0:
            continue
        if found is None or index < found[0] or (index == found[0] and len(verb) > len(found[1])):
            found = (index, verb)
    return found if found else (-1, "")


def _narrative_action(text: str) -> tuple[int, str]:
    found: tuple[int, str] | None = None
    for verb in _PRICE_NARRATIVE:
        index = text.find(verb)
        if index < 0:
            continue
        if found is None or index < found[0] or (index == found[0] and len(verb) > len(found[1])):
            found = (index, verb)
    return found if found else (-1, "")


def _split_headline(text: str) -> tuple[str, str, str]:
    """选出主体说得通的谓语。叙述后面再接发布、出台这类动作时，宾语停在那之前。"""
    options: list[tuple[int, int, str, str]] = []
    for index, verb in (_tuple_action(text), _narrative_action(text), _directive_action(text)):
        if index < 0 or not verb or not _subject_ok(text[:index]):
            continue
        options.append((index, -len(verb), verb, text[:index]))
    if not options:
        return "", "", ""
    options.sort()
    index, _neg, verb, lead = options[0]
    rest = text[index + len(verb):]
    obj = rest
    if verb in _PRICE_NARRATIVE:
        later, later_verb = _tuple_action(rest)
        if later > 0 and later_verb in _GLUE_FOLLOWUPS:
            obj = rest[:later]
    return lead, verb, obj.strip("，。！？、；：")


def _clause_continues_object(text: str) -> bool:
    """逗号后面没有新的主体和动作，就是上一句的宾语，不是另一件事。"""
    body = _compact(text)
    if not body or _starts_fragment(body) or _watch_or_list_fragment(body):
        return False
    for glue in _RUNON_GLUE:
        if body.startswith(glue):
            return False
    for finder in (_tuple_action, _narrative_action, _directive_action):
        index, verb = finder(body)
        if not verb:
            continue
        if finder is _directive_action or _subject_ok(body[:index]):
            return False
    return True


def _attach_following_object(body: str) -> str:
    """不超过 28 字时，不要把「定调，固态电池…」收成只有「定调」。"""
    parts = [part.strip() for part in _CLAUSE.split(body) if part.strip()]
    if len(parts) < 2 or len(_compact(body)) > _TITLE_HARD:
        return body
    lead, action, obj = _split_headline(parts[0])
    if not action or obj or not _subject_ok(lead):
        return body
    extra: list[str] = []
    for part in parts[1:]:
        if not _clause_continues_object(part):
            break
        extra.append(part)
    if not extra:
        return body
    joined = parts[0] + "".join(extra)
    if len(joined) > _TITLE_HARD:
        return body
    if _display_clause_ok(joined) or _title_ok(joined):
        return joined
    return body


def _ends_mid_clause(text: str) -> bool:
    """停在「集体」或半个词上，说明这一句还没写完。"""
    body = _compact(text)
    if not body:
        return True
    if any(body.endswith(tail) for tail in _FALLBACK_BAD_TAILS):
        return True
    return _ends_abruptly(body)


def _cut_glued_sentence(text: str) -> str:
    """价格叙述后面又接下一句时，停在分句边界，不能把「集体涨价」切成「集体」。"""
    body = _compact(text)
    if not body:
        return ""
    index, verb = _narrative_action(body)
    if index < 0 or not _subject_ok(body[:index]):
        return body
    rest = body[index + len(verb):]
    later, later_verb = _tuple_action(rest)
    if later <= 0 or later_verb not in _UNGLUE_VERBS:
        return body
    head = body[:index + len(verb) + later].rstrip("，。！？、；：")
    gap = body[index + len(verb):index + len(verb) + later]
    boundary = max((gap.rfind(char) for char in "，。！？、；"), default=-1)
    if boundary >= 0:
        return body[:index + len(verb) + boundary].rstrip("，。！？、；：")
    if _ends_mid_clause(head):
        return body[:index + len(verb) + later + len(later_verb)]
    return head


def _title_ok(title: str) -> bool:
    """完整、有主体和动作、不超过 20 字，且不是半句话或词中间截断。"""
    text = _compact(title)
    if (
        _title_incoherent(text)
        or _has_file_debris(text)
        or _watch_or_list_fragment(text)
        or _misordered_title(text)
        or _surface_block_reason(text)
    ):
        return False
    if not text or len(text) > _TITLE_LIMIT or text in BROAD_SECTORS:
        return False
    if _starts_fragment(text) or _ends_abruptly(text):
        return False
    if re.search(r"超(?!过|预|标|额|出|市)", text):
        return False
    lead, action, obj = _split_headline(text)
    if not action or not _subject_ok(lead):
        return False
    tail = text[len(lead) + len(action) + len(obj):].strip("，。！？、；：")
    return not tail


def _fit_object(base: str, obj: str) -> str:
    obj = obj.strip("，。！？、；：")
    if not obj:
        return base
    full = base + obj
    if len(full) <= _TITLE_HARD and not _ends_abruptly(obj) and not _ends_mid_number(full):
        return full
    # 宾语本身是半个词（例如停在「政」）时整段丢掉，不要只削掉最后一个字。
    if not _ends_abruptly(obj):
        room = _TITLE_HARD - len(base)
        chunk = obj[:room] if room > 0 else ""
        while chunk and (
            _ends_abruptly(base + chunk)
            or _ends_split_verb(base + chunk)
            or _ends_mid_number(base + chunk)
            or chunk[-1] in "的和与及"
        ):
            chunk = chunk[:-1]
        if len(chunk) >= 2:
            return base + chunk
    head = re.split(r"[和与及]", obj, maxsplit=1)[0]
    if head and head != obj and len(base) + len(head) <= _TITLE_HARD and not _ends_abruptly(head):
        return base + head
    return base


def _repair_headline(text: str) -> str:
    body = _TIME_PREFIX.sub("", _drop_leading_fragment(_compact(text)))
    if not body:
        return ""
    options: list[tuple[int, int, str, str]] = []
    for index, verb in (_tuple_action(body), _narrative_action(body)):
        if index < 0 or not verb:
            continue
        subject = _normalize_subject(body[:index])
        if _subject_ok(subject):
            options.append((index, -len(verb), subject, verb))
    if not options:
        return ""
    options.sort()
    index, _neg, subject, verb = options[0]
    rest = body[index + len(verb):]
    obj = rest
    if verb in _PRICE_NARRATIVE:
        later, later_verb = _tuple_action(rest)
        if later > 0 and later_verb in _GLUE_FOLLOWUPS:
            obj = rest[:later]
    base = f"{subject}{verb}"
    if len(base) > _TITLE_HARD or _ends_abruptly(base) or _ends_split_verb(base):
        return ""
    return _fit_object(base, obj)


_SIGNING_PARTY = re.compile(
    r"([\u4e00-\u9fffA-Za-z0-9]{2,10})[与和及]"
    r"([\u4e00-\u9fffA-Za-z0-9]{2,10}?)"
    r"(?=战略合作|签署|签订|签约|达成|合作)"
)


def _signing_parties(text: str) -> tuple[str, str]:
    """签约、战略合作标题里用与/和连起来的双方。"""
    body = _compact(text)
    body = re.sub(r"([\u4e00-\u9fffA-Za-z0-9]{2,10})[：:]与", r"\1与", body)
    if not any(cue in body for cue in ("签约", "签署", "签订", "战略合作")):
        return "", ""
    match = _SIGNING_PARTY.search(body)
    if not match:
        return "", ""
    left, right = match.group(1), match.group(2)
    if left in _NOT_SUBJECT or right in _NOT_SUBJECT or left in _SOURCE_NAMES or right in _SOURCE_NAMES:
        return "", ""
    return left, right


def _signing_title(text: str) -> str:
    """双方都留下。长标题被从「与」切开时，收成一句仍带对手方的签约标题。"""
    left, right = _signing_parties(text)
    if not left or not right:
        return ""
    body = _compact(text)
    if "签约" in body and "战略合作" in body:
        title = f"{left}与{right}战略合作签约"
    elif "签约" in body:
        title = f"{left}与{right}签约"
    elif "签署" in body:
        title = f"{left}与{right}签署战略合作" if "战略合作" in body else f"{left}与{right}签署合作"
    elif "签订" in body:
        title = f"{left}与{right}签订战略合作" if "战略合作" in body else f"{left}与{right}签订合作"
    elif "战略合作" in body:
        title = f"{left}与{right}战略合作签约"
    else:
        return ""
    if _display_clause_ok(title) or _title_ok(title):
        return title
    return ""


def _keeps_signing_parties(title: str, source: str) -> bool:
    left, right = _signing_parties(source)
    if not left:
        return True
    return left in title and right in title


def _prefer_signed(chosen: str, body: str, signed: str) -> str:
    """完整分句里双方都在就留原句。被截短或丢掉一方时，改用带双方的签约标题。"""
    if not signed:
        return chosen
    if not _keeps_signing_parties(chosen, body):
        return signed
    compact_body = _compact(body)
    compact_chosen = _compact(chosen)
    if compact_chosen and compact_chosen != compact_body and len(compact_chosen) > len(signed):
        return signed
    return chosen


def _dropped_signing_party(title: str, source: str) -> bool:
    text = _compact(title)
    if not text or not any(cue in text for cue in ("签约", "签署", "签订", "战略合作")):
        return False
    for raw in source.split("\n"):
        left, right = _signing_parties(raw)
        if left and right and (left not in text or right not in text):
            return True
    return False


def _fit_clause(clause: str) -> str:
    compact = _drop_leading_fragment(_strip_source_prefix(clause))
    if not compact or _watch_or_list_fragment(compact):
        return ""
    if _display_clause_ok(compact) or _title_ok(compact):
        return compact
    repaired = _repair_headline(compact)
    if repaired and (_display_clause_ok(repaired) or _title_ok(repaired)):
        return repaired
    return ""


def _representative_clause(text: str) -> str:
    body = _prepare_headline(text)
    if not body:
        return ""
    chosen = ""
    for part in _CLAUSE.split(body):
        fitted = _fit_clause(part)
        if fitted:
            chosen = fitted
            break
    return _prefer_signed(chosen, body, _signing_title(body))


def _title_cache_key(fingerprint: str) -> str:
    return f"title-v{_TITLE_CACHE_VERSION}:{fingerprint}"


def _purge_stale_title_cache() -> None:
    prefix = f"title-v{_TITLE_CACHE_VERSION}:"
    for key in [key for key in _LLM_CACHE if not str(key).startswith(prefix)]:
        _LLM_CACHE.pop(key, None)


def _hint(now: datetime, trading: date, chosen: date, fallback: bool, updated_hm: str) -> str:
    clock = f" · 更新于 {updated_hm}" if updated_hm else ""
    if fallback:
        return f"暂无{_month_day(trading)}数据，显示{_month_day(chosen)}{clock}"
    if _auction_still_ahead(now, chosen):
        pending = f"交易日 {_month_day(chosen)} · 等待竞价" if chosen != now.date() else "等待竞价"
        return f"{pending}{clock}"
    if chosen != now.date():
        return f"交易日 {_month_day(chosen)}{clock}"
    return f"更新于 {updated_hm}" if updated_hm else ""


def _month_day(day: date) -> str:
    return f"{day.month}月{day.day}日"


def _is_section_label(name: object) -> bool:
    """栏目、材料分区，以及没有动作的点评、重点，都不是事件。"""
    text = _TRAILING_ELLIPSIS.sub("", _compact(str(name or "")))
    if not text:
        return False
    if text == "重点" or _SECTION_MATERIAL.fullmatch(text):
        return True
    if _tuple_action(text)[0] >= 0 or _narrative_action(text)[0] >= 0:
        return False
    return "点评" in text or text.startswith("重点")


_COMPANY_SUFFIXES = (
    "股份有限公司", "有限责任公司", "有限公司", "集团股份", "控股集团",
    "科技", "股份", "集团", "控股", "银行", "证券", "医药", "电子", "能源",
    "实业", "材料", "生物", "光电", "通信", "传媒", "地产", "汽车", "化工",
    "矿业", "资源", "国际", "电力", "新材", "信息", "网络", "软件", "智能",
    "环保", "装备", "机械", "半导体",
)
# 这些字出现在标题里，说明不是光秃的一个名字。
_NAME_GLUE = frozenset("的了在将把被从对而或已正再又也还就都与和")


def _is_source_only(text: str) -> bool:
    """整段标题只是媒体名，例如央视财经、财联社电、金十快讯。"""
    body = _compact(text)
    for tail in ("快讯", "报道", "消息", "电讯", "电", "讯"):
        if body.endswith(tail) and len(body) - len(tail) >= 2:
            body = body[: -len(tail)]
            break
    return body in _SOURCE_NAMES


def _is_bare_entity(text: str) -> bool:
    """整段标题只是公司或证券简称，里面没有动作。"""
    body = _compact(text)
    if not body or _tuple_action(body)[0] >= 0 or _narrative_action(body)[0] >= 0:
        return False
    if any(char.isdigit() for char in body) or any(char in _NAME_GLUE for char in body):
        return False
    if not re.fullmatch(r"[\u4e00-\u9fffA-Za-z]{2,16}", body):
        return False
    if any(body.endswith(suffix) and len(body) > len(suffix) for suffix in _COMPANY_SUFFIXES):
        return True
    return len(body) <= 8


def _title_name_reject_reason(text: str) -> str:
    if _is_source_only(text):
        return "只有来源名"
    if _is_bare_entity(text):
        return "只有主体没有动作"
    return ""


def _is_commentary(text: str) -> bool:
    """侧面佐证、看好、关注、建议这类说法，没有主体和动作时不是事件。"""
    body = _compact(text)
    if not body or not any(cue in body for cue in _COMMENTARY_CUES):
        return False
    lead, action, _obj = _split_headline(body)
    return not (action and _subject_ok(lead))


def _proper_opener(prefix: str) -> str:
    """冒号前的公司名也算专名。媒体名和「公司」本身不是。"""
    parts = [part for part in re.split(r"[：:]", prefix.strip("：:")) if part]
    while parts and (
        parts[-1] in _GENERIC_SUBJECT_HEADS
        or parts[-1] in _SOURCE_NAMES
        or parts[-1] in _NOT_SUBJECT
    ):
        parts.pop()
    if not parts:
        return ""
    opener = parts[-1]
    if len(opener) < 2 or len(opener) > 12:
        return ""
    if not re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9]+", opener):
        return ""
    if opener in _NOT_SUBJECT or opener in _STOP or opener in _GENERIC_TOKENS or opener in _SOURCE_NAMES:
        return ""
    return opener


def _named_subject_swapped(title: str, source: str) -> bool:
    """标题用「公司」换掉了原标题里的专名。对照原始标题，不先按分句清掉冒号前的公司名。"""
    text = _compact(title)
    generic = next((word for word in _GENERIC_SUBJECT_HEADS if text.startswith(word)), "")
    if not generic or not source:
        return False
    rest = text[len(generic):]
    if len(rest) < 2:
        return False
    for raw in source.split("\n"):
        src = _compact(raw)
        if len(src) < 4:
            continue
        for needle in (text, rest):
            index = src.find(needle)
            if index < 2:
                continue
            opener = _proper_opener(src[:index])
            if opener and opener not in text:
                return True
    return False


def _llm_title_reject_reason(title: str, source: str = "") -> str:
    """模型标题的拒绝原因。空字符串表示可以上屏。

    字数只卡到 48。标题中间的年份、价格（2026、13.1元）不是截断；
    只有整句停在数字上才算数字被截断。
    """
    text = _compact(title)
    if not text or text == "事件":
        return "空标题"
    if _is_commentary(text) or _watch_or_list_fragment(text):
        return "不是具体事件"
    if _misordered_title(text):
        return "语序颠倒"
    blocked = _surface_block_reason(text)
    if blocked:
        return blocked
    name_reason = _title_name_reject_reason(text)
    if name_reason:
        return name_reason
    debris = _file_debris_reason(text)
    if debris:
        return debris
    if _title_incoherent(text):
        return "标题不连贯"
    if len(text) < 6:
        return "过短"
    if len(text) > _LLM_TITLE_MAX:
        return "超过48字"
    if _is_section_label(text):
        return "栏目标签"
    if text[0].isdigit():
        return "以数字开头"
    if any(char in text for char in "【】[]"):
        return "含括号"
    if "..." in text or "…" in text:
        return "省略号"
    if _starts_fragment(text):
        return "半句话"
    if _ends_abruptly(text):
        return "结尾不完整"
    if _ends_split_verb(text):
        return "动词被截断"
    if _ends_mid_number(text):
        return "数字被截断"
    if sum(1 for char in text if "\u4e00" <= char <= "\u9fff") < 2:
        return "中文不足"
    if _cut_glued_sentence(text) != text:
        return "粘连了下一句"
    if len(text) > _TITLE_LIMIT and _core_event_clause(text) != text:
        return "标题过长"
    if source and _named_subject_swapped(text, source):
        return "主体被换成泛称"
    if source and _dropped_signing_party(text, source):
        return "缺少合作方"
    if source and _cuts_any(text, [line for line in source.split("\n") if line.strip()]):
        return "截断过短"
    lead, action, obj = _split_headline(text)
    if not (action and obj and _subject_ok(lead) and _concrete_subject(lead)):
        return "缺少主体、动作或宾语"
    return ""


def _llm_title_ok(title: str) -> bool:
    """模型标题：完整中文，有主体、动作和宾语，且不是钟点、标签或半句。"""
    return _llm_title_reject_reason(title) == ""


def _has_emoji(text: str) -> bool:
    for char in text:
        code = ord(char)
        if code >= 0x1F000 or 0x2600 <= code <= 0x27BF:
            return True
        if code > 0x2100 and unicodedata.category(char) == "So":
            return True
    return False


def _fallback_title_ok(name: object) -> bool:
    """回退标题比关键词聚类更严：表情、斜杠串票、残词和点评起句都不能上屏。"""
    if not _cleaned_display_ok(name):
        return False
    text = _compact(str(name or ""))
    if _title_name_reject_reason(text):
        return False
    if _has_emoji(text) or _CJK_SLASH.search(text):
        return False
    if any(text.startswith(prefix) for prefix in _FALLBACK_BAD_OPENINGS):
        return False
    if any(text.endswith(tail) for tail in _FALLBACK_BAD_TAILS):
        return False
    lead, action, _obj = _split_headline(text)
    return not action or _concrete_subject(lead)


def _cleaned_display_ok(name: object) -> bool:
    """关键词回退不要求满 6 字，但钟点、括号、省略号和栏目标题不能上屏。"""
    text = _compact(str(name or ""))
    if (
        _title_incoherent(text)
        or _has_file_debris(text)
        or _is_commentary(text)
        or _watch_or_list_fragment(text)
        or _misordered_title(text)
        or _surface_block_reason(text)
    ):
        return False
    if _core_event_clause(text) != text:
        return False
    lead, action, _obj = _split_headline(text)
    if action and not _concrete_subject(lead):
        return False
    if not _display_name_ok(text) or _is_section_label(text):
        return False
    if text[0].isdigit() or any(char in text for char in "【】[]"):
        return False
    return "..." not in text and "…" not in text


def _event_title_sources(event: dict) -> list[str]:
    sources = [str(item) for item in (event.get("headlines") or [])]
    sources.append(str(event.get("headline") or ""))
    sources.append(str(event.get("name") or ""))
    return [text for text in sources if text.strip()]


def _drops_known_action(short: str, full: str) -> bool:
    """截完以后原句里的动作不在了，这句不能上屏。"""
    _lead, verb, _obj = _split_headline(full)
    return bool(verb) and verb not in _compact(short)


def _is_harmful_cutoff(short: str, long: str) -> bool:
    """半截标题：切开了英文或数字，丢掉了动作，或把没超长的句子收成只有动作。"""
    short_text = _compact(short)
    long_text = _compact(long)
    if not short_text or len(short_text) < 4 or len(short_text) >= len(long_text):
        return False
    if not long_text.startswith(short_text):
        return False
    if _cut_glued_sentence(long_text) == short_text:
        return False
    rest = long_text[len(short_text):]
    if re.fullmatch(r"[.。…]+", rest):
        return False
    nxt = rest[0]
    prev = short_text[-1]
    if _is_ascii_alnum(prev) and _is_ascii_alnum(nxt):
        return True
    if _drops_known_action(short_text, long_text):
        return True
    return nxt not in "，。！？、；：" and len(long_text) <= _TITLE_HARD and not _ends_abruptly(long_text)


def _cuts_any(name: str, sources: list[str]) -> bool:
    for raw in sources:
        if _is_harmful_cutoff(name, raw) or _is_harmful_cutoff(name, _cleaned_headline(raw)):
            return True
    return False


def _fallback_cleaned_name(event: dict) -> str:
    sources = _event_title_sources(event)
    found: list[str] = []
    for text in sources:
        cleaned = _cleaned_headline(text)
        if cleaned and _fallback_title_ok(cleaned) and cleaned not in found:
            found.append(cleaned)
    if not found:
        return ""
    found.sort(key=len, reverse=True)
    for candidate in found:
        if any(candidate != other and _is_harmful_cutoff(candidate, other) for other in found):
            continue
        return candidate
    return found[0]


def _ensure_shown_name(event: dict) -> None:
    sources = _event_title_sources(event)
    name = str(event.get("name") or "")
    if event.get("_llm_title_applied") and _llm_title_ok(name) and not _cuts_any(name, sources):
        return
    if _fallback_title_ok(name) and not _cuts_any(name, sources):
        return
    event["name"] = _fallback_cleaned_name(event)
    event.pop("_llm_title_applied", None)


def _drop_cutoff_duplicates(events: list[dict]) -> list[dict]:
    """半截标题如果已经有完整的那一条，就不再单列。"""
    texts = [_event_title_sources(event) for event in events]
    names = [_compact(str(event.get("name") or "")) for event in events]
    drop: set[int] = set()
    for index, name in enumerate(names):
        if not name:
            continue
        for other, blobs in enumerate(texts):
            if other == index:
                continue
            if names[other] and (_is_harmful_cutoff(name, names[other]) or _cuts_any(name, blobs)):
                drop.add(index)
                break
    return [event for index, event in enumerate(events) if index not in drop]


def _has_ashare_mapping(event: dict) -> bool:
    if any(str(name).strip() for name in (event.get("concepts") or [])):
        return True
    for item in event.get("mentioned_stocks") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "").strip() or str(item.get("key") or "").strip():
            return True
    return False


def _has_strong_market_cue(text: str) -> bool:
    if any(org in text for org in _ORG_SUBJECTS):
        return True
    return any(cue in text for cue in _STRONG_MARKET)


def _non_market_unmapped(event: dict) -> bool:
    """奖项、文体、体育，又没有 A 股概念或个股时，不进热榜。"""
    if _has_ashare_mapping(event):
        return False
    text = _event_text(event)
    if any(cue in text for cue in _HARD_NON_MARKET):
        return True
    return any(cue in text for cue in _SOFT_NON_MARKET) and not _has_strong_market_cue(text)


def _keep_shown_event(event: dict) -> bool:
    if _non_market_unmapped(event):
        return False
    name = str(event.get("name") or "")
    if event.get("_llm_title_applied"):
        return _llm_title_ok(name)
    return _fallback_title_ok(name)


def _cached_title_usable(cached: dict) -> bool:
    if cached.get("_miss"):
        return True
    title = str(cached.get("title") or "")
    return not title or _llm_title_ok(title)


def _event_source_text(event: dict) -> str:
    parts = [str(event.get("headline") or ""), str(event.get("name") or "")]
    parts.extend(str(item) for item in (event.get("headlines") or []))
    return "\n".join(part for part in parts if part)


def _title_prompt(events: list[dict], *, retry: bool) -> str:
    blocks = []
    for event in events:
        headlines = [
            str(item).strip()
            for item in (event.get("headlines") or [])
            if str(item).strip()
        ]
        if not headlines:
            fallback = str(event.get("headline") or event.get("name") or "").strip()
            headlines = [fallback] if fallback else []
        blocks.append(f"{event.get('_batch_id')}. " + " / ".join(headlines))
    retry_line = "上一轮标题不合格，请重写。\n" if retry else ""
    names = "、".join(_dictionary_names())
    return (
        "下面每条是一件与资本市场有关的资讯。为每一条写展示标题。"
        "只输出 JSON："
        '{"events":[{"id":"1","title":"","concepts":[],"category":"",'
        '"direction":"利好或利空","importance":"重大或重要或一般或琐碎"}]}。'
        "title 必须是完整的中文标题，尽量不超过28个字，写完整，不要为了压字数把词截断，"
        "写明主体、动作和宾语，"
        "例如美联储降息25基点。不要半句话，不要时间戳，不要【】，不要省略号，"
        "不要把「前法官」「前总统」写成「在前法官」「在前总统」。不要文件名、连字符后的日期和#。"
        "不要以数字或钟点开头，不要以叠加、和之前、根据、与开头，不要在词中间截断。"
        "不要把下一句粘进来。用叠加、接续把原因和事件粘在一起时，只写后面的核心事件，不超过20个字。"
        "不要用「事件」当标题。不要输出重点、点评或栏目标签。"
        "不要只写媒体名或公司名，标题里要有动作。"
        "不要把已经点名的公司换成「公司」，立讯精密仍写立讯精密。"
        "不要把动词放在主体前面，例如不要写成「重申…白宫…签署」。"
        "标题写不下时停在分句边界，不要把动词从句子中间切掉。"
        "不要在英文字母或数字中间截断。没超过28个字的标题不要截短，不要只留下动作、把宾语丢掉。"
        "不要在标题末尾写天风计算机这类券商组名，不要写观点更新这类研报标签。"
        "冒号前面如果是公司名，要保留公司名。公司名后面接着「公司」或「本公司」时，用公司名替换，不要写成「公司」。"
        "不要以持续关注开头，不要写1）或①这样的列表序号。"
        "不要写招商电新称、计算机称、研究称这类券商前缀。"
        "签约、签署和战略合作要保留双方名称。"
        "不要写侧面佐证、印证、看好、关注或把握这类没有具体动作的点评。"
        "国务院、工信部、发改委、国资委、央行或公司名后面的布局、建议、推荐是事件，要保留。"
        "主体必须写明，例如美联储、国务院、财政部。不要只写同比或数据显示。"
        f"category 只能是：{'、'.join(CATEGORY_NAMES)}。"
        "importance 按分量：央行利率、国家级政策、战争制裁、大宗商品冲击、龙头公司重大事项是重大；"
        "寻常涨价、获批、订单是重要。"
        f"concepts 只在该事件还没有概念时填写，并且只能从这些名称里选：{names}。"
        "对不上就留空。不要新造概念。不要用来源栏目，栏目名里的快报不是概念。"
        "概念要和标题里的国家地区一致，不要把伊朗或中东标成俄乌。"
        "direction 表示对 A 股相关方向是利好还是利空。"
        "没有把握就留空，不要编造标题里没出现的事实。\n"
        f"{retry_line}\n"
        + "\n".join(blocks)
    )


def _request_titles(events: list[dict], *, retry: bool) -> str:
    from app.news.service import _llm_text

    try:
        return _llm_text(_title_prompt(events, retry=retry), max_tokens=_LLM_TITLE_TOKENS)
    except Exception as exc:  # noqa: BLE001
        logger.info("热门事件标题生成失败: %s", exc)
        return ""


def _label_with_llm(events: list[dict]) -> None:
    from app.news.config import llm_extract_enabled

    if not events or not llm_extract_enabled():
        return
    from app.news.service import _reserve_llm_call

    _purge_stale_title_cache()
    pending: list[dict] = []
    for event in events[:_LLM_TITLE_BATCH]:
        cache_key = _title_cache_key(str(event.get("_fp") or ""))
        cached = _LLM_CACHE.get(cache_key)
        if isinstance(cached, dict) and _cached_title_usable(cached):
            if not cached.get("_miss"):
                _apply_llm(event, cached)
            continue
        if cached is not None:
            _LLM_CACHE.pop(cache_key, None)
        event["_batch_id"] = str(len(pending) + 1)
        pending.append(event)
    if not pending or not _reserve_llm_call():
        return
    first = _parse_title_batch(_request_titles(pending, retry=False), pending)
    failed = [
        event for event in pending
        if not _accept_title(event, first.get(str(event.get("_batch_id"))))
    ]
    if not failed:
        return
    second: dict[str, dict] = {}
    if _reserve_llm_call():
        second = _parse_title_batch(_request_titles(failed, retry=True), failed)
    for event in failed:
        batch_id = str(event.get("_batch_id"))
        parsed = second.get(batch_id)
        if _accept_title(event, parsed):
            continue
        fallback = parsed or first.get(batch_id)
        if fallback:
            blank = dict(fallback)
            blank["title"] = ""
            _apply_llm(event, blank)
        _LLM_CACHE[_title_cache_key(str(event.get("_fp") or ""))] = {"_miss": True}


def _accept_title(event: dict, parsed: dict | None) -> bool:
    title = str((parsed or {}).get("title") or "")
    reason = _llm_title_reject_reason(title, _event_source_text(event))
    if reason:
        logger.info("热门事件标题未采用: %s（%s）", title or "∅", reason)
        return False
    _apply_llm(event, parsed)
    _LLM_CACHE[_title_cache_key(str(event.get("_fp") or ""))] = parsed
    return True


def _parse_title_batch(raw: str, events: list[dict]) -> dict[str, dict]:
    from app.news.extract import _llm_object

    payload = _llm_object(raw)
    if not payload:
        return {}
    rows = payload.get("events")
    if not isinstance(rows, list):
        keys = ("title", "concepts", "category", "direction", "importance", "stocks")
        rows = [payload] if len(events) == 1 and any(key in payload for key in keys) else []
    normalized = [row for row in rows if isinstance(row, dict)]
    missing_ids = normalized and all(not str(row.get("id") or "").strip() for row in normalized)
    if missing_ids and len(normalized) == len(events):
        for event, row in zip(events, normalized, strict=True):
            row["id"] = event.get("_batch_id")
    found: dict[str, dict] = {}
    by_id = {str(event.get("_batch_id")): event for event in events}
    for row in normalized:
        batch_id = str(row.get("id") or "").strip()
        if not batch_id and len(events) == 1:
            batch_id = str(events[0].get("_batch_id") or "")
        event = by_id.get(batch_id)
        if event is None:
            continue
        parsed = _parse_llm_payload(row, _event_source_text(event))
        if parsed:
            found[batch_id] = parsed
    return found


def _parse_llm_payload(payload: dict, text: str) -> dict | None:
    title = _compact(str(payload.get("title") or ""))
    allowed = set(_dictionary_names())
    concepts = []
    concept_directions: dict[str, str] = {}
    for item in payload.get("concepts") or payload.get("sectors") or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            direct = str(item.get("direction") or "")
        else:
            name, direct = str(item or "").strip(), ""
        in_text = bool(name) and name in text and _fine_concept(name, name)
        if name and name not in concepts and (in_text or name in allowed):
            concepts.append(name)
            if direct in {"利好", "利空"}:
                concept_directions[name] = direct
    stocks = []
    for item in payload.get("stocks") or []:
        if isinstance(item, str):
            name, code, direct = item.strip(), "", ""
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            code = str(item.get("code") or item.get("symbol") or "").strip()
            direct = str(item.get("direction") or "")
        else:
            continue
        if (name and name in text) or (code and code in text):
            row = {"name": name, "code": code}
            if direct in {"利好", "利空"}:
                row["direction"] = direct
            stocks.append(row)
    category = str(payload.get("category") or "").strip()
    if category not in CATEGORY_NAMES:
        category = ""
    direction = str(payload.get("direction") or "").strip()
    if direction not in {"利好", "利空"}:
        direction = ""
    importance = str(payload.get("importance") or "").strip()
    if importance not in _IMPORTANCE_POINTS:
        importance = ""
    if not title and not concepts and not stocks and not category and not importance:
        return None
    return {
        "title": title,
        "category": category,
        "direction": direction,
        "importance": importance,
        "concepts": concepts[:_CONCEPT_LIMIT],
        "concept_directions": concept_directions,
        "stocks": stocks,
    }


def _apply_llm(event: dict, parsed: dict) -> None:
    title = _compact(str(parsed.get("title") or ""))
    if _llm_title_ok(title):
        event["name"] = title
        event["_llm_title_applied"] = True
    category = str(parsed.get("category") or "")
    if category in CATEGORY_NAMES:
        event["category"] = category
    direction = str(parsed.get("direction") or "")
    if direction in {"利好", "利空"}:
        event["direction"] = direction
    importance = str(parsed.get("importance") or "")
    if importance == "琐碎":
        importance = "一般"
    if importance in _IMPORTANCE_POINTS:
        event["importance"] = importance
    had_concepts = any(str(name).strip() for name in (event.get("concepts") or []))
    if had_concepts:
        for name in parsed.get("concepts") or []:
            if name not in event["concepts"] and len(event["concepts"]) < _CONCEPT_LIMIT:
                event["concepts"].append(name)
        texts = [str(event.get("name") or ""), str(event.get("headline") or "")]
        texts.extend(str(item) for item in (event.get("headlines") or []))
        event["concepts"] = _merge_mapped_concepts(
            _relevant_concepts(
                Counter({str(name): 1 for name in event.get("concepts") or []}),
                texts,
                _subject_set(str(event.get("name") or ""), []),
            ),
            "\n".join(texts),
        )
    else:
        allowed = set(_dictionary_names())
        picked: list[str] = []
        for name in parsed.get("concepts") or []:
            if name in allowed and name not in picked:
                picked.append(str(name))
            if len(picked) >= _CONCEPT_LIMIT:
                break
        event["concepts"] = picked
    directions = parsed.get("concept_directions") or {}
    event["mapping"] = [
        {
            "name": name,
            "kind": "concept",
            "direction": directions.get(name) or event.get("direction") or "利好",
        }
        for name in event["concepts"]
    ]
    if direction in {"利好", "利空"}:
        for stock in event.get("mentioned_stocks") or []:
            stock["direction"] = direction
    known = {item["key"] for item in event.get("mentioned_stocks") or []}
    known_names = {item["name"] for item in event.get("mentioned_stocks") or []}
    for stock in parsed.get("stocks") or []:
        code = str(stock.get("code") or "").strip()
        name = str(stock.get("name") or "").strip()
        key = code or name
        direct = str(stock.get("direction") or event.get("direction") or "利好")
        if direct not in {"利好", "利空"}:
            direct = "利好"
        if not key or key in known or name in known_names:
            continue
        known.add(key)
        event["mentioned_stocks"].append({
            "key": key,
            "name": name or key,
            "mentions": 1,
            "source_count": 1,
            "direction": direct,
        })
