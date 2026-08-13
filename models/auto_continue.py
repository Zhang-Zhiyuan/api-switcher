from dataclasses import asdict, dataclass, field
import re
from typing import Optional


DEFAULT_INCOMPLETE_PATTERNS = [
    r"(?i)^(?!.*\bno\s+remaining\s+todo(s)?\b).*\b(?:todo|to do|fixme|wip|work in progress)\b",
    r"(?i)\bnot (?:yet )?complete\b",
    r"(?i)(incomplete|unfinished|not finished|not done|partially done|partial implementation)",
    r"(?i)(?=.*\b(project|task|work|implementation|feature|change|repo|repository)\b)(?=.*\b(?:not|isn't|is not|hasn't|has not|haven't|have not)\b.{0,120}\b(?:actually|really|truly|fully|completely)?\b.{0,120}\b(?:complete|completed|done|finished|ready)\b)(?=.*\b(?:continue|resume|keep\s+going|keep\s+running|carry\s+on|run\s+again|keep\s+working)\b).*",
    r"(?i)(?=.*\b(project|task|work|implementation|feature|change|repo|repository)\b)(?=.*\b(?:actually|really|truly|fully|completely)\b.{0,80}\b(?:not|isn't|is not|hasn't|has not|haven't|have not)\b.{0,80}\b(?:complete|completed|done|finished|ready)\b)(?=.*\b(?:continue|resume|keep\s+going|keep\s+running|carry\s+on|run\s+again|keep\s+working)\b).*",
    r"(?i)(?<!\bno\s)(will|need to|should|must).{0,80}(implement|add|create|fix|test|verify|wire|integrate|finish|complete|clean up)",
    r"(?i)(next|following|follow-up|remaining) steps?:",
    r"(?is)<task-notification>.*<status>\s*(killed|stopped|failed|terminated)\s*</status>.*</task-notification>",
    r"(?i)background command .{0,160}\b(was )?(stopped|killed|terminated|interrupted)\b",
    r"(?i)to be (done|completed|implemented)",
    r"(?i)(stub|placeholder|scaffold)(s)? remain",
    r"(?i)(only partially|partially implemented)",
    r"(?i)(scaffold(ing)? only|not production[- ]ready|not ready (for|to))",
    r"(?i)not yet (implemented|wired|tested|verified|covered|supported|working|ready)",
    r"(?i)(has|have) not been (implemented|wired|tested|verified|covered|supported|completed|finished)",
    r"(?i)(isn't|aren't) (implemented|wired|tested|verified|covered|supported|complete|finished|ready)",
    r"(?i)still (is|are)? ?(missing|incomplete|unfinished|pending|stubbed|placeholder)",
    r"(?i)^(?!.*\bno\s+remaining\b).*remaining (work|tasks|implementation|steps|plan|items)",
    r"(?i)(work|tasks|implementation|steps|items) (remain|remaining)",
    r"(?i)still (need|needs|needed|left|missing)",
    r"(?i)still (to do|todo|pending|open)",
    r"(?i)left to (do|implement|finish|complete)",
    r"(?i)(?<!\bno\s)(need|needs|needed|required|requires?) (to )?(implement|finish|complete|fix|test|verify|wire|integrate|add|update|clean up)",
    r"(?i)\bno\s+(?:test\s+)?failures?\s+(?:has|have)\s+been\s+(?:fixed|resolved|addressed)\b(?:\s+yet)?",
    r"(?i)needs? (more work|implementation|cleanup|testing|verification)",
    r"(?i)(missing|lacking) (implementation|tests|verification|coverage|config|documentation|files|support|integration)",
    r"(?i)not (tested|verified|validated|covered|wired|integrated|implemented|supported)",
    r"(?i)without (tests|verification|validation|coverage|implementation|support|integration)",
    r"(?i)no (tests|verification|validation|coverage|implementation|support|integration|acceptance record)",
    r"(?i)(next steps?|follow-up|remaining steps?|todo|to do).{0,120}(implement|finish|complete|fix|test|verify|wire|integrate|add|update|build|package|ship)",
    r"(?i)(should|recommend|recommended|worth) (next|continue|still).{0,120}(implement|finish|complete|fix|test|verify|wire|integrate|add|update|build|package)",
    r"(?i)(should|shall|would\s+you\s+like(?:\s+me)?\s+to|do\s+you\s+want(?:\s+me)?\s+to|can\s+I|may\s+I).{0,160}(continue|resume|keep\s+going|carry\s+on).{0,80}[\?\uFF1F]\s*$",
    r"(?i)(^|\n)\s*(continue|resume|keep\s+going|carry\s+on)\s*[\?\uFF1F]\s*$",
    r"(?i)(type|say|reply(?:\s+with)?).{0,80}continue.{0,80}(to\s+continue|$)\s*$",
    r"(?i)press\s+(enter|return)\s+to\s+continue\s*[:\uFF1A]?\s*$",
    r"\u8981.{0,40}\u7ee7\u7eed.{0,80}[\?\uFF1F]\s*$",
    r"(\u56de\u590d|\u8f93\u5165|\u53d1\u9001).{0,50}\u7ee7\u7eed.{0,80}(\u4ee5\u7ee7\u7eed|\u7ee7\u7eed|$)\s*$",
    r"\u8fd8\u6ca1\u5b8c",
    r"\u5c1a\u672a\u5b8c",
    r"\u6ca1\u6709.{0,20}(\u5b8c\u6210|\u7ed3\u675f|\u6536\u5c3e)",
    r"(?=.*(\u9879\u76ee|\u4efb\u52a1|\u5de5\u4f5c|\u529f\u80fd|\u5b9e\u73b0|\u6539\u52a8))(?=.*(\u6ca1\u6709|\u6ca1|\u672a|\u5c1a\u672a|\u5e76\u672a|\u8fd8\u6ca1).{0,50}(\u771f\u6b63|\u5b9e\u9645|\u5b8c\u5168|\u5f7b\u5e95|\u5b8c\u6574)?.{0,50}(\u5b8c\u6210|\u505a\u5b8c|\u7ed3\u675f|\u6536\u5c3e))(?=.*(\u53ef\u4ee5|\u80fd|\u8fd8\u80fd|\u5e94\u8be5|\u9700\u8981|\u5efa\u8bae).{0,50}(\u7ee7\u7eed|\u7eed\u8dd1|\u63a5\u7740\u8dd1|\u5f80\u4e0b\u8dd1|\u8dd1\u4e0b\u53bb)).*",
    r"(?=.*(\u9879\u76ee|\u4efb\u52a1|\u5de5\u4f5c|\u529f\u80fd|\u5b9e\u73b0|\u6539\u52a8))(?=.*(\u5b9e\u9645\u4e0a|\u5176\u5b9e|\u4e8b\u5b9e\u4e0a|\u771f\u6b63|\u5b8c\u5168|\u5f7b\u5e95).{0,50}(\u6ca1\u6709|\u6ca1|\u672a|\u5c1a\u672a|\u5e76\u672a|\u8fd8\u6ca1).{0,50}(\u5b8c\u6210|\u505a\u5b8c|\u7ed3\u675f|\u6536\u5c3e))(?=.*(\u53ef\u4ee5|\u80fd|\u8fd8\u80fd|\u5e94\u8be5|\u9700\u8981|\u5efa\u8bae).{0,50}(\u7ee7\u7eed|\u7eed\u8dd1|\u63a5\u7740\u8dd1|\u5f80\u4e0b\u8dd1|\u8dd1\u4e0b\u53bb)).*",
    r"\u672a\u5b8c\u6210",
    r"\u6ca1\u5b8c\u6210",
    r"\u4e0d\u5b8c\u6574",
    r"\u53ea(\u5b8c\u6210|\u662f).{0,30}(\u521d\u6b65|\u90e8\u5206|\u4e34\u65f6|\u9aa8\u67b6|\u5360\u4f4d)",
    r"\u90e8\u5206\u5b8c\u6210",
    r"\u4ec5\u5b8c\u6210",
    r"\u53ea\u662f.{0,30}(\u521d\u6b65|\u90e8\u5206|\u4e34\u65f6|\u9aa8\u67b6|\u5360\u4f4d)",
    r"\u8fd8\u4e0d\u80fd.{0,30}(\u4ea4\u4ed8|\u4f7f\u7528|\u5de5\u4f5c|\u901a\u8fc7|\u8dd1\u901a)",
    r"\u8fd8\u65e0\u6cd5.{0,30}(\u4ea4\u4ed8|\u4f7f\u7528|\u5de5\u4f5c|\u901a\u8fc7|\u8dd1\u901a)",
    r"\u8fd8(\u6ca1\u6709|\u6ca1).{0,20}(\u5b8c\u6210|\u5b8c|\u7ed3\u675f|\u6536\u5c3e)",
    r"\u8fd8(\u6709|\u9700|\u9700\u8981|\u5f97|\u8981).{0,80}(\u505a|\u5b9e\u73b0|\u8865|\u8865\u4e0a|\u4fee|\u4fee\u590d|\u6d4b|\u9a8c\u8bc1|\u6e05\u7406|\u6536\u5c3e)",
    r"\u5269\u4f59.{0,80}(\u5b8c\u6210|\u5b9e\u73b0|\u5de5\u4f5c|\u4efb\u52a1|\u90e8\u5206|\u8ba1\u5212|\u7b56\u7565|\u767e\u5206\u6bd4|\u5b8c\u6574\u5ea6)",
    r"\u8fd8\u5dee.{0,80}(%|\u767e\u5206\u6bd4|\u5b8c\u6574\u5ea6|\u5b8c\u6210|\u5b9e\u73b0|\u5de5\u4f5c)",
    r"\u5269\u4e0b.{0,80}(\u9700\u8981|\u5f85|\u672a|\u5de5\u4f5c|\u4efb\u52a1|\u5b9e\u73b0|\u5b8c\u6210)",
    r"\u8ddd\u79bb.{0,80}(\u5b8c\u6210|\u4ea4\u4ed8|\u53ef\u7528|\u7a33\u5b9a).{0,80}(\u8fd8|\u4ecd|\u6709|\u5dee)",
    r"(?<!\u65e0)(?<!\u4e0d)(\u9700|\u9700\u8981|\u5e94|\u5e94\u8be5|\u8fd8\u8981|\u8fd8\u9700|\u5fc5\u987b).{0,80}(\u8865|\u8865\u9f50|\u8865\u4e0a|\u5b9e\u73b0|\u4fee\u590d|\u9a8c\u8bc1|\u6d4b\u8bd5|\u8c03\u8bd5|\u5b8c\u5584|\u6536\u5c3e)",
    r"\u5f85(\u5b9e\u73b0|\u5b8c\u6210|\u5904\u7406|\u4fee\u590d|\u9a8c\u8bc1|\u6d4b\u8bd5|\u8865\u5145|\u8865\u9f50|\u63a5\u5165|\u96c6\u6210|\u8c03\u8bd5)",
    r"\u672a(\u5b9e\u73b0|\u5b8c\u6210|\u5904\u7406|\u4fee\u590d|\u9a8c\u8bc1|\u6d4b\u8bd5|\u8865\u5145|\u8865\u9f50|\u63a5\u5165|\u96c6\u6210|\u8c03\u8bd5)",
    r"\u6ca1\u6709.{0,30}(\u5b9e\u73b0|\u9a8c\u8bc1|\u6d4b\u8bd5|\u8dd1\u901a|\u63a5\u5165|\u96c6\u6210|\u6253\u5305|\u6784\u5efa|\u90e8\u7f72|\u8bb0\u5f55)",
    r"\u6ca1.{0,30}(\u5b9e\u73b0|\u9a8c\u8bc1|\u6d4b\u8bd5|\u8dd1\u901a|\u63a5\u5165|\u96c6\u6210|\u6253\u5305|\u6784\u5efa|\u90e8\u7f72|\u8bb0\u5f55)",
    r"\u4ecd\u7136.{0,80}(\u9700\u8981|\u5f85|\u672a|\u6ca1|\u7f3a|\u5c11|\u4e0d\u652f\u6301|\u4e0d\u5b8c\u6574)",
    r"\u4ecd.{0,80}(\u9700\u8981|\u5f85|\u672a|\u6ca1|\u7f3a|\u5c11|\u4e0d\u652f\u6301|\u4e0d\u5b8c\u6574)",
    r"\u6682\u672a.{0,80}(\u5b9e\u73b0|\u5b8c\u6210|\u9a8c\u8bc1|\u6d4b\u8bd5|\u63a5\u5165|\u652f\u6301)",
    r"\u5c1a\u9700.{0,80}(\u5b9e\u73b0|\u5b8c\u6210|\u9a8c\u8bc1|\u6d4b\u8bd5|\u8865\u5145|\u5904\u7406)",
    r"\u7f3a(\u5c11|\u5931)?.{0,80}(\u5b9e\u73b0|\u6587\u4ef6|\u6d4b\u8bd5|\u9a8c\u8bc1|\u914d\u7f6e|\u4f9d\u8d56|\u6587\u6863|\u7b56\u7565)",
    r"\u6b20\u7f3a.{0,80}(\u5b9e\u73b0|\u6d4b\u8bd5|\u9a8c\u8bc1|\u914d\u7f6e|\u6587\u6863|\u7b56\u7565)",
    r"\u6f0f\u4e86.{0,80}(\u5b9e\u73b0|\u5904\u7406|\u6d4b\u8bd5|\u9a8c\u8bc1|\u66f4\u65b0)",
    r"\u6ca1\u6765\u5f97\u53ca.{0,80}(\u5b9e\u73b0|\u4fee\u590d|\u6d4b\u8bd5|\u9a8c\u8bc1)",
    r"\u63a5\u4e0b\u6765.{0,80}(\u9700\u8981|\u9700|\u5f97|\u8981|\u7ee7\u7eed).{0,80}(\u5b9e\u73b0|\u8865|\u4fee|\u4fee\u590d|\u6d4b|\u6d4b\u8bd5|\u9a8c\u8bc1|\u5b8c\u6210|\u5904\u7406|\u4f18\u5316)",
    r"\u4e0b\u4e00\u6b65.{0,120}(\u5b9e\u73b0|\u8865|\u4fee|\u6d4b|\u9a8c\u8bc1|\u5b8c\u6210|\u8dd1\u901a|\u6253\u5305|\u6784\u5efa)",
    r"\u540e\u7eed.{0,120}(\u5b9e\u73b0|\u8865|\u4fee|\u6d4b|\u9a8c\u8bc1|\u5b8c\u6210|\u8dd1\u901a|\u6253\u5305|\u6784\u5efa)",
    r"\u6700\u503c\u5f97\u505a.{0,120}(\u8865|\u5b9e\u73b0|\u4fee|\u6d4b|\u9a8c\u8bc1|\u5b8c\u6210)",
    r"\u5efa\u8bae\u7ee7\u7eed.{0,80}(\u5b9e\u73b0|\u8865|\u4fee|\u6d4b|\u9a8c\u8bc1|\u5b8c\u6210)",
    r"\u5efa\u8bae\u4e0b\u4e00\u6b65.{0,80}(\u5b9e\u73b0|\u8865|\u4fee|\u6d4b|\u9a8c\u8bc1|\u5b8c\u6210)",
    r"\u5982\u679c\u4f60\u540c\u610f.{0,120}\u6211.{0,20}(\u4e0b\u4e00\u6b65|\u63a5\u4e0b\u6765).{0,120}(\u5f00\u59cb|\u6267\u884c|\u63a8\u8fdb|\u7ee7\u7eed|\u5b9e\u73b0|\u4fee|\u6d4b|\u8bad\u7ec3|\u91cd\u8bad)",
    r"(\u4e0b\u4e00\u6b65|\u63a5\u4e0b\u6765).{0,120}(\u5f00\u59cb|\u6267\u884c|\u63a8\u8fdb|\u8bad\u7ec3|\u91cd\u8bad).{0,80}(\u4e13\u9879|\u5b9e\u9a8c|\u4efb\u52a1|\u8bc4\u4f30|\u9a8c\u8bc1|\u5b9e\u73b0|\u4fee\u590d)",
    r"\u5df2\u7ecf.{0,40}\u4f46.{0,120}(\u8fd8|\u4ecd|\u9700|\u672a|\u6ca1|\u7f3a|\u5f85)",
    r"\u867d\u7136.{0,80}\u4f46.{0,120}(\u8fd8|\u4ecd|\u9700|\u672a|\u6ca1|\u7f3a|\u5f85)",
    r"\u5f53\u524d.{0,80}(\u53ea|\u4ec5|\u4ecd|\u5c1a|\u8fd8).{0,80}(\u652f\u6301|\u5b9e\u73b0|\u5b8c\u6210|\u8986\u76d6)",
    r"\u8fd9\u8fd8\u4e0d\u662f.{0,80}(\u5b8c\u6574|\u6700\u7ec8|\u53ef\u4ea4\u4ed8|\u751f\u4ea7\u53ef\u7528)",
    r"\u8fd8\u4e0d\u662f.{0,80}(\u5b8c\u6574|\u6700\u7ec8|\u53ef\u4ea4\u4ed8|\u751f\u4ea7\u53ef\u7528)",
    r"\u6ca1\u6709.{0,80}(\u5b8c\u6574|\u771f\u6b63|\u5b9e\u9645|\u771f\u5b9e).{0,80}(\u5b9e\u73b0|\u9a8c\u6536|\u9a8c\u8bc1|\u6d4b\u8bd5|\u8bb0\u5f55|\u652f\u6301)",
    r"(\u8981\u4e0d\u8981|\u662f\u5426).{0,80}\u7ee7\u7eed",
    r"(?<!\u4e0d)(?<!\u65e0)(\u9700\u8981|\u8fd8\u8981|\u53ef\u4ee5).{0,80}\u7ee7\u7eed",
    r"\u7ee7\u7eed\u6267\u884c.{0,100}[\?\uff1f]?$",
    r"(\u8bf7|\u56de\u590d|\u8f93\u5165).{0,40}\u7ee7\u7eed.{0,40}(\u4ee5|\u6765)?\u7ee7\u7eed\s*$",
    r"\u6309\s*(enter|\u56de\u8f66)\s*(\u952e)?\s*(\u7ee7\u7eed|\u4ee5\u7ee7\u7eed)\s*[:\uFF1A]?\s*$",
]


# Explicit terminal signals are intentionally limited to affirmative clauses.
# They are evaluated against incomplete signals by match position so a later
# resolved conclusion can override an earlier historical issue without treating
# "must verify that everything is complete" as a completion claim.
DEFAULT_TERMINAL_COMPLETION_PATTERNS = [
    r"(?i)(?:^|[.!?;]\s+|\s[-*•]\s+)no\s+(?:unfinished|remaining|pending|outstanding)\s+(?:work|tasks?|items?|steps?|todos?)\b(?:\s+(?:remain|remains|remaining|left))?",
    r"(?i)(?:^|[.!?;]\s+|\s[-*•]\s+)(?:everything|all(?:\s+(?:requested|required|remaining))?\s+(?:work|tasks?|items?|steps?|changes?)|(?:the\s+)?(?:task|work|implementation|changes?))\s+(?:(?:is|are|was|were|has\s+been|have\s+been)\s+)?(?:fully\s+)?(?:complete|completed|done|finished|implemented)\b",
    r"(?i)(?:^|[.!?;]\s+|\s[-*•]\s+)(?:all|the)\s+tests?\s+(?:pass|passed|are\s+passing|have\s+passed)\b",
    r"(?i)(?:^|[.!?;]\s+|\s[-*•]\s+)no\s+(?:test\s+)?failures?\b(?=\s*(?:remain(?:ing|s)?|(?:were\s+)?(?:found|detected|observed|reported)|exist(?:s)?|[.!?;]|$))",
    r"(?i)(?:^|[.!?;]\s+|\s[-*•]\s+)no\s+need\s+to\s+(?:continue|implement|add|create|fix|test|verify|wire|integrate|finish|complete|clean\s+up)\b",
    r"(?i)(?:^|[.!?;]\s+|\s[-*•]\s+)(?:the\s+)?previously\s+missing\b.{0,120}\b(?:added|implemented|fixed|restored|resolved|now\s+pass(?:es|ed)?)\b",
    r"(?:^|[。！？；，]\s*)(?:也|并)?(?:没有|无)(?:任何)?(?:未完成|剩余|待处理|待办)(?:的)?(?:工作|任务|事项|步骤|项)?",
    r"(?:^|[。！？；，]\s*)(?:全部|所有|整体|任务|工作|改动|修改).{0,20}(?:已完成|完成了|已实现|已处理|已收尾)",
    r"(?:^|[。！？；，]\s*)已?完成了?(?:全部|所有).{0,20}(?:修改|改动|工作|任务|事项)",
    r"(?:^|[。！？；，]\s*)(?:所有|全部)?测试.{0,20}(?:均|都|已|全部).{0,10}(?:通过|成功)",
    r"(?:^|[。！？；，]\s*)(?:没有|无).{0,20}(?:测试失败|失败的测试|失败项)(?:[。！？；，]|$)",
    r"(?:^|[。！？；，]\s*)无需.{0,20}(?:继续|实现|修复|测试|验证|收尾)",
]


DEFAULT_BLOCKER_PATTERNS = [
    r"(?i)\u9700\u8981(\u4f60|\u7528\u6237).{0,20}(\u786e\u8ba4|\u63d0\u4f9b|\u51b3\u5b9a|\u9009\u62e9|\u6388\u6743|\u767b\u5f55|\u5bc6\u94a5|\u51ed\u636e|\u5bc6\u7801|token|api\s*key)",
    r"(?i)(\u7b49\u5f85|\u9700\u8981).{0,30}(\u8f93\u5165|\u786e\u8ba4|\u6388\u6743|\u767b\u5f55|\u5bc6\u94a5|\u51ed\u636e|\u5bc6\u7801|token|api\s*key)",
    r"(\u9700\u8981|\u7b49\u5f85).{0,30}(\u4f60|\u7528\u6237).{0,30}(\u51b3\u7b56|\u62cd\u677f|\u9009\u62e9|\u786e\u8ba4)",
    r"\u8bf7.{0,10}(\u9009\u62e9|\u786e\u8ba4|\u63d0\u4f9b|\u6388\u6743|\u51b3\u5b9a)",
    r"(\u9700\u8981|\u8bf7|\u7b49\u5f85|\u7b49\u4f60|\u7b49\u7528\u6237|\u7531\u4f60|\u7531\u7528\u6237).{0,40}(\u8f93\u5165|\u786e\u8ba4|\u9009\u62e9|\u51b3\u5b9a|\u6388\u6743|\u6279\u51c6|\u540c\u610f|\u63d0\u4f9b|\u8865\u5145|\u56de\u590d|\u544a\u77e5|\u6307\u5b9a)",
    r"(\u8bf7\u9009\u62e9|\u8bf7\u786e\u8ba4|\u8bf7\u63d0\u4f9b|\u8bf7\u6388\u6743|\u8bf7\u51b3\u5b9a|\u9700\u8981\u4f60\u786e\u8ba4|\u9700\u8981\u4f60\u9009\u62e9|\u9700\u8981\u7528\u6237\u786e\u8ba4|\u7b49\u5f85\u7528\u6237|\u7b49\u4f60\u786e\u8ba4)",
    r"(\u7f3a\u5c11|\u7f3a\u5931|\u627e\u4e0d\u5230|\u4e0d\u5b58\u5728).{0,20}(\u6587\u4ef6|\u914d\u7f6e|\u8def\u5f84|\u547d\u4ee4|\u4f9d\u8d56|\u53c2\u6570|\u4fe1\u606f|\u51ed\u8bc1|\u6743\u9650|API|api|key|token|\u6a21\u578b|\u8d26\u53f7|\u76ee\u5f55|\u73af\u5883\u53d8\u91cf)",
    r"(\u6ca1\u6709\u6743\u9650|\u6743\u9650\u4e0d\u8db3|(?<!\u65e0\u987b)\u65e0\u6cd5|(?<!\u4e0d\u5b58\u5728)\u4e0d\u80fd).{0,40}(\u7ee7\u7eed|\u5199\u5165|\u8bbf\u95ee|\u4fee\u6539|\u6267\u884c)?",
    r"(?i)(cannot|can't|unable to|blocked).{0,80}(credential|secret|password|token|api key|login|sign in|permission|approval)",
    r"(?i)(need|requires?).{0,80}(your confirmation|user confirmation|credential|secret|password|token|api key|login|sign in|permission|approval)",
    r"(?i)(need|requires?|waiting for|blocked by).{0,80}(your|user).{0,40}(input|decision|choice|confirmation|approval|permission|credentials?)",
    r"(?i)(please|can you|could you).{0,80}(choose|select|confirm|approve|provide|authorize|decide|log in|sign in)",
    r"(?i)which (option|approach|method|profile|configuration|config) (do you|would you like|should I)",
    r"(?i)(missing|not found|does not exist).{0,80}(file|config|path|command|dependency|parameter|credential|permission|api key|token|model|account|directory|environment variable)",
    r"(?i)(waiting for|blocked by).{0,80}(user|your).{0,40}(decision|choice|confirmation|approval|credential|secret|password|token|api key)",
    r"(?i)(deploy|publish|release).{0,80}(confirmation|approval|credential|secret|password|token|api key|login|sign in)",
    r"(?i)(?:blocked|cannot|can't|unable|requires?|needs?|waiting).{0,80}(?:payment|billing|delete user data|destructive action|approval)",
    r"(?i)(?:payment|billing).{0,80}(?:required|failed|issue|blocked|approval|credential|login)",
]


LEGACY_GENERATED_PATTERNS_TO_DROP = {
    r"(?i)(still|remaining|todo|wip|work in progress|not (yet )?complete)",
    r"(?i)(error|failed|cannot|unable to|blocked by)",
    r"(?i)(missing|not found|does not exist)",
    r"(\u8981\u4e0d\u8981|\u662f\u5426|\u9700\u8981|\u8fd8\u8981|\u53ef\u4ee5).{0,80}\u7ee7\u7eed",
    r"(?i)(payment|billing|delete user data|destructive)",
}


DEFAULT_PERMISSION_AUTO_APPROVE_TOOLS = ["Bash", "Edit", "MultiEdit", "Write", "NotebookEdit"]
DEFAULT_TRAINING_CONTINUE_PROMPT = (
    "\u8bf7\u68c0\u67e5\u5f53\u524d\u6df1\u5ea6\u5b66\u4e60/\u6a21\u578b\u8bad\u7ec3\u4efb\u52a1\u7684\u6700\u65b0\u8bc4\u4f30\u7ed3\u679c\u3001"
    "\u8bad\u7ec3\u65e5\u5fd7\u3001\u6307\u6807\u548c\u6a21\u578b\u4ea7\u7269\u3002\u5982\u679c\u5c1a\u672a\u8fbe\u5230\u6211\u914d\u7f6e\u7684\u76ee\u6807\uff0c"
    "\u8bf7\u7ee7\u7eed\u8bad\u7ec3\u3001\u8c03\u53c2\u3001\u6539\u8fdb\u6a21\u578b\u6216\u8865\u5145\u9a8c\u8bc1\uff1b"
    "\u5982\u679c\u5df2\u7ecf\u8fbe\u5230\u76ee\u6807\uff0c\u8bf7\u5728\u6700\u7ec8\u56de\u590d\u4e2d\u5199\u51fa TRAINING_TARGET_MET\uff0c"
    "\u5e76\u5217\u51fa\u5173\u952e\u6307\u6807\u548c\u4ea7\u7269\u8def\u5f84\u3002"
)
DEFAULT_TRAINING_PROMPT_TEMPLATE_KEY = "general"
TRAINING_PROMPT_TEMPLATES = [
    {
        "key": "general",
        "name": "通用训练评估",
        "description": "适合已有明确指标但任务类型不固定的训练项目。",
        "prompt": (
            "任务类型：通用深度学习/机器学习训练。\n"
            "目标指标：请把这里改成具体要求，例如 val_acc >= 0.95、F1 >= 0.90、loss <= 0.20。\n"
            "检查：读取最新训练日志、验证集/测试集指标、最佳 checkpoint、模型产物路径和最近一次实验配置。\n"
            "如果未达标：定位瓶颈，继续训练、调参、清理数据或补充验证，并记录新的指标。\n"
            "如果达标：最终回复中写出 TRAINING_TARGET_MET，并列出关键指标、模型/checkpoint 路径和复现实验命令。"
        ),
    },
    {
        "key": "classification",
        "name": "分类/表格模型",
        "description": "关注 accuracy、F1、AUC、precision、recall、类别不均衡和阈值。",
        "prompt": (
            "任务类型：分类或表格模型训练。\n"
            "目标指标：请把这里改成具体要求，例如 AUC >= 0.92、macro-F1 >= 0.88、recall >= 0.90。\n"
            "检查：验证集/测试集指标、混淆矩阵、类别分布、阈值、过拟合迹象和最佳模型文件。\n"
            "如果未达标：优先尝试学习率、正则化、类别权重、采样策略、阈值搜索、特征处理或模型结构调整。\n"
            "如果达标：最终回复中写出 TRAINING_TARGET_MET，并列出指标、阈值、模型路径和可复现实验命令。"
        ),
    },
    {
        "key": "vision",
        "name": "视觉检测/分割",
        "description": "适合目标检测、语义分割、实例分割、OCR 等视觉训练。",
        "prompt": (
            "任务类型：计算机视觉训练，例如 detection、segmentation、OCR 或 image classification。\n"
            "目标指标：请把这里改成具体要求，例如 mAP50 >= 0.80、mIoU >= 0.75、val_loss 持续下降。\n"
            "检查：验证集指标、样例可视化、漏检/误检类别、数据增强、输入尺寸、best checkpoint 和导出模型。\n"
            "如果未达标：调整数据增强、类别采样、学习率、冻结层、输入分辨率、训练轮数或后处理阈值。\n"
            "如果达标：最终回复中写出 TRAINING_TARGET_MET，并列出指标、模型路径、导出格式和推理验证结果。"
        ),
    },
    {
        "key": "llm_finetune",
        "name": "LLM 微调/评测",
        "description": "适合 SFT、LoRA、RAG 评测、指令跟随和 benchmark 通过率。",
        "prompt": (
            "任务类型：LLM 微调、LoRA/SFT、RAG 评测或文本生成模型改进。\n"
            "目标指标：请把这里改成具体要求，例如 eval_loss <= 目标值、benchmark 通过率 >= 90%、人工样例全部通过。\n"
            "检查：eval loss、perplexity、benchmark 结果、失败样例、幻觉/格式错误、adapter/checkpoint 路径。\n"
            "如果未达标：继续微调或改进数据、prompt、RAG 检索、负样本、训练步数、学习率和评测集覆盖。\n"
            "如果达标：最终回复中写出 TRAINING_TARGET_MET，并列出指标、checkpoint/adapter 路径和关键评测样例。"
        ),
    },
    {
        "key": "forecasting",
        "name": "回归/时序预测",
        "description": "适合 MAE、MSE、RMSE、MAPE、分位数损失等回归或预测任务。",
        "prompt": (
            "任务类型：回归、排序或时间序列预测。\n"
            "目标指标：请把这里改成具体要求，例如 RMSE <= 目标值、MAE <= 目标值、MAPE <= 10%。\n"
            "检查：验证/测试集误差、分桶误差、异常样本、数据泄漏、特征窗口、归一化方式和最佳模型文件。\n"
            "如果未达标：调整特征、窗口长度、损失函数、正则化、学习率、模型容量或异常值处理。\n"
            "如果达标：最终回复中写出 TRAINING_TARGET_MET，并列出指标、模型路径、数据版本和复现实验命令。"
        ),
    },
]
TRAINING_PROMPT_TEMPLATE_KEYS = {
    template["key"]
    for template in TRAINING_PROMPT_TEMPLATES
}
DEFAULT_TRAINING_COMPLETION_PATTERNS = [
    r"(?i)\bTRAINING_TARGET_MET\b",
    r"(?i)\b(training|evaluation|model)\b.{0,80}\b(target|goal|criteria|requirement)s?\b.{0,80}\b(met|reached|satisfied|passed)\b",
    r"(?i)\b(met|reached|satisfied|passed)\b.{0,80}\b(training|evaluation|model)\b.{0,80}\b(target|goal|criteria|requirement)s?\b",
    r"\u8bad\u7ec3\u76ee\u6807\u5df2\u8fbe\u6210",
    r"\u6307\u6807\u5df2\u8fbe\u6807",
    r"\u8bc4\u4f30\u7ed3\u679c\u5df2\u8fbe\u6807",
    r"(\u51c6\u786e\u7387|\u7cbe\u5ea6|\u53ec\u56de\u7387|F1|loss|AUC).{0,80}(\u8fbe\u6807|\u8fbe\u5230\u8981\u6c42|\u6ee1\u8db3\u8981\u6c42)",
]
DEFAULT_TRAINING_NOT_MET_PATTERNS = [
    r"(?i)\b(?:not|hasn't|has\s+not|haven't|have\s+not|wasn't|was\s+not|isn't|is\s+not|yet\s+to)\b.{0,80}\bTRAINING_TARGET_MET\b",
    r"(?i)\bTRAINING_TARGET_MET\b.{0,80}\b(?:not|isn't|is\s+not|wasn't|was\s+not|hasn't|has\s+not|yet)\b",
    r"(?i)\b(?:training|evaluation|model)\b.{0,80}\b(?:target|goal|criteria|requirement)s?\b.{0,80}\b(?:not|isn't|wasn't|hasn't|haven't|failed\s+to)\b.{0,40}\b(?:met|reached|satisfied|passed)\b",
    r"(?i)\b(?:not|hasn't|has\s+not|haven't|have\s+not|failed\s+to)\b.{0,60}\b(?:met|reached|satisfied|passed)\b.{0,80}\b(?:training|evaluation|model)\b.{0,80}\b(?:target|goal|criteria|requirement)s?\b",
    r"(?:训练目标|训练指标|评估目标|评估指标).{0,30}(?:未|没有|尚未|还未).{0,20}(?:达成|达到|达标|满足)",
    r"(?:未|没有|尚未|还未).{0,30}(?:达成|达到|满足).{0,30}(?:训练目标|训练指标|评估目标|评估指标)",
]
DEFAULT_TRAINING_SKIP_PATTERNS = [
    r"(?i)\bTRAINING_NOT_APPLICABLE\b",
    r"(?i)\bNOT_A_TRAINING_TASK\b",
    r"(?i)\b(no|not a|not an)\s+(training|model training|deep learning)\s+(task|job|project)\b",
    r"(?i)\btraining\s+(guard|auto-continue)\s+(not applicable|does not apply)\b",
    r"\u5f53\u524d\u4e0d\u662f.{0,40}(\u8bad\u7ec3|\u6a21\u578b|\u6df1\u5ea6\u5b66\u4e60).{0,40}(\u4efb\u52a1|\u9879\u76ee)",
    r"\u4e0d\u662f.{0,40}(\u8bad\u7ec3|\u6a21\u578b|\u6df1\u5ea6\u5b66\u4e60).{0,40}(\u4efb\u52a1|\u9879\u76ee)",
    r"\u6ca1\u6709.{0,40}(\u8bad\u7ec3|\u6a21\u578b|\u8bc4\u4f30).{0,40}(\u4efb\u52a1|\u7ed3\u679c|\u4e0a\u4e0b\u6587)",
    r"\u65e0.{0,20}(\u8bad\u7ec3|\u6a21\u578b\u8bad\u7ec3|\u6df1\u5ea6\u5b66\u4e60).{0,20}(\u4efb\u52a1|\u9879\u76ee)",
    r"\u8bad\u7ec3\u7eed\u8dd1\u4e0d\u9002\u7528",
]
DEFAULT_TRAINING_CONTEXT_PATTERNS = [
    r"(?i)\b(train|training|trained|fine[- ]?tune|finetune|finetuning|epoch|epochs)\b",
    r"(?i)\b(eval|evaluation|evaluate|validation|validating|val[_ -]?acc|val[_ -]?loss|test set|dev set|holdout)\b",
    r"(?i)\b(accuracy|acc|loss|auc|f1|precision|recall|mae|mse|rmse|bleu|rouge|perplexity|metric|metrics)\b",
    r"(?i)\b(checkpoint|ckpt|weights|model artifact|model output|best model|early stopping)\b",
    r"(?i)\b(dataset|dataloader|batch size|learning rate|lr|optimizer|scheduler|gradient|overfit|underfit)\b",
    r"(?i)\b(wandb|tensorboard|mlflow|huggingface|pytorch|tensorflow|keras|sklearn|xgboost|lightgbm)\b",
    r"(?i)\b(model|classifier|regressor|network|nn|llm)\b.{0,80}\b(train|training|fine[- ]?tune|eval|evaluation|metric|accuracy|loss|checkpoint|weights)\b",
    r"(?i)\b(train|training|fine[- ]?tune|eval|evaluation|metric|accuracy|loss|checkpoint|weights)\b.{0,80}\b(model|classifier|regressor|network|nn|llm)\b",
    r"\u8bad\u7ec3|\u5fae\u8c03|\u8c03\u53c2|\u8fed\u4ee3|\u8f6e\u6b21|epoch|\u6b65\u6570|\u5b66\u4e60\u7387|\u4f18\u5316\u5668",
    r"\u8bc4\u4f30|\u9a8c\u8bc1\u96c6|\u6d4b\u8bd5\u96c6|\u6307\u6807|\u51c6\u786e\u7387|\u7cbe\u5ea6|\u53ec\u56de\u7387|\u635f\u5931|AUC|F1|loss",
    r"\u6743\u91cd|\u68c0\u67e5\u70b9|\u6700\u4f73\u6a21\u578b|\u8fc7\u62df\u5408|\u6b20\u62df\u5408|\u6570\u636e\u96c6",
    r"(\u6a21\u578b|\u7f51\u7edc|\u5206\u7c7b\u5668).{0,80}(\u8bad\u7ec3|\u5fae\u8c03|\u8bc4\u4f30|\u6307\u6807|\u51c6\u786e\u7387|\u635f\u5931|\u68c0\u67e5\u70b9|\u6743\u91cd)",
    r"(\u8bad\u7ec3|\u5fae\u8c03|\u8bc4\u4f30|\u6307\u6807|\u51c6\u786e\u7387|\u635f\u5931|\u68c0\u67e5\u70b9|\u6743\u91cd).{0,80}(\u6a21\u578b|\u7f51\u7edc|\u5206\u7c7b\u5668)",
]
BOOL_SETTING_FIELDS = {
    "enabled",
    "apply_to_subagents",
    "conservative_mode",
    "error_recovery_enabled",
    "git_auto_snapshot",
    "git_auto_push",
    "git_snapshot_on_start",
    "git_snapshot_on_recovery",
    "auto_approve_permission_requests",
    "auto_approve_bash",
    "training_auto_continue_enabled",
}
STRING_SETTING_FIELDS = {
    "continuation_prompt",
    "training_continue_prompt",
    "training_prompt_template_key",
}
INTEGER_SETTING_FIELDS = {
    "max_continuations",
    "max_stagnant_continuations",
    "max_error_recoveries",
    "error_retry_initial_delay_seconds",
    "error_retry_max_delay_seconds",
    "auto_approve_max_per_session",
}


def _coerce_bool_setting(value, default):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return value


def _coerce_int_setting(value, default):
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if value is None:
        return default

    text = str(value).strip()
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    return default


def _coerce_string_setting(value, default):
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _merge_unique_patterns(patterns: list[str] | None, defaults: list[str]) -> list[str]:
    """Return user patterns plus missing built-ins, dropping unsafe generated defaults."""
    merged: list[str] = []
    seen: set[str] = set()
    source = patterns if isinstance(patterns, list) else []

    for pattern in source + defaults:
        value = str(pattern).strip()
        if not value or value in LEGACY_GENERATED_PATTERNS_TO_DROP or value in seen:
            continue
        merged.append(value)
        seen.add(value)

    return merged


def _merge_unique_strings(values: list[str] | None, defaults: list[str]) -> list[str]:
    """Return user strings plus defaults, deduplicated case-insensitively."""
    merged: list[str] = []
    seen: set[str] = set()
    source = values if isinstance(values, list) else []

    for item in source + defaults:
        value = str(item).strip()
        key = value.casefold()
        if value and key not in seen:
            merged.append(value)
            seen.add(key)

    return merged


def _resolve_training_prompt_template_key(value) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    folded = normalized.casefold()
    for template in TRAINING_PROMPT_TEMPLATES:
        if template["key"].casefold() == folded or template["name"].casefold() == folded:
            return template["key"]
    return None


def training_prompt_template_by_key(key: str | None) -> dict[str, str]:
    """Return a built-in training prompt template, falling back to the general template."""
    normalized = _resolve_training_prompt_template_key(key)
    for template in TRAINING_PROMPT_TEMPLATES:
        if template["key"] == normalized:
            return template
    return TRAINING_PROMPT_TEMPLATES[0]


def training_prompt_template_by_name(name: str | None) -> dict[str, str]:
    """Return a built-in training prompt template by display name."""
    return training_prompt_template_by_key(name)


@dataclass
class AutoContinueSettings:
    """Settings for auto-continue functionality."""

    enabled: bool = False
    max_continuations: int = 100
    max_stagnant_continuations: int = 3
    continuation_prompt: str = "Please continue from where you left off. Complete any remaining work."
    apply_to_subagents: bool = False
    conservative_mode: bool = True
    error_recovery_enabled: bool = False
    max_error_recoveries: int = 3
    error_retry_initial_delay_seconds: int = 5
    error_retry_max_delay_seconds: int = 60
    training_auto_continue_enabled: bool = False
    training_prompt_template_key: str = DEFAULT_TRAINING_PROMPT_TEMPLATE_KEY
    training_continue_prompt: str = DEFAULT_TRAINING_CONTINUE_PROMPT
    git_auto_snapshot: bool = True
    git_auto_push: bool = False
    git_snapshot_on_start: bool = True
    git_snapshot_on_recovery: bool = True
    auto_approve_permission_requests: bool = False
    auto_approve_max_per_session: int = 0
    auto_approve_bash: bool = True
    auto_approve_tools: list[str] = field(default_factory=lambda: list(DEFAULT_PERMISSION_AUTO_APPROVE_TOOLS))
    incomplete_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_INCOMPLETE_PATTERNS))
    blocker_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKER_PATTERNS))

    def validate(self) -> tuple[bool, str]:
        """Validate settings. Returns (is_valid, error_message)."""
        for field_name in BOOL_SETTING_FIELDS:
            if not isinstance(getattr(self, field_name), bool):
                return False, f"{field_name} must be a boolean"

        if not isinstance(self.max_continuations, int) or self.max_continuations < -1:
            return False, "max_continuations must be -1 or a non-negative integer"

        if self.max_continuations > 100:
            return False, "max_continuations too large (max: 100)"

        if (
            isinstance(self.max_stagnant_continuations, bool)
            or not isinstance(self.max_stagnant_continuations, int)
            or self.max_stagnant_continuations < 0
        ):
            return False, "max_stagnant_continuations must be a non-negative integer"

        if self.max_stagnant_continuations > 20:
            return False, "max_stagnant_continuations too large (max: 20)"

        if not isinstance(self.max_error_recoveries, int) or self.max_error_recoveries < 0:
            return False, "max_error_recoveries must be a non-negative integer"

        if self.max_error_recoveries > 10:
            return False, "max_error_recoveries too large (max: 10)"

        if (
            not isinstance(self.error_retry_initial_delay_seconds, int)
            or self.error_retry_initial_delay_seconds < 1
        ):
            return False, "error_retry_initial_delay_seconds must be a positive integer"

        if self.error_retry_initial_delay_seconds > 300:
            return False, "error_retry_initial_delay_seconds too large (max: 300)"

        if (
            not isinstance(self.error_retry_max_delay_seconds, int)
            or self.error_retry_max_delay_seconds < 1
        ):
            return False, "error_retry_max_delay_seconds must be a positive integer"

        if self.error_retry_max_delay_seconds > 600:
            return False, "error_retry_max_delay_seconds too large (max: 600)"

        if self.error_retry_initial_delay_seconds > self.error_retry_max_delay_seconds:
            return False, "error_retry_initial_delay_seconds cannot exceed error_retry_max_delay_seconds"

        if not isinstance(self.training_continue_prompt, str):
            return False, "training_continue_prompt must be a string"

        if self.training_auto_continue_enabled and not self.training_continue_prompt.strip():
            return False, "training_continue_prompt cannot be empty when training auto-continue is enabled"

        if len(self.training_continue_prompt) > 8000:
            return False, "training_continue_prompt is too long (max: 8000 characters)"

        if not isinstance(self.training_prompt_template_key, str):
            return False, "training_prompt_template_key must be a string"

        if self.training_prompt_template_key not in TRAINING_PROMPT_TEMPLATE_KEYS:
            return False, "training_prompt_template_key is not a known template"

        if not isinstance(self.auto_approve_permission_requests, bool):
            return False, "auto_approve_permission_requests must be a boolean"

        if not isinstance(self.auto_approve_max_per_session, int) or self.auto_approve_max_per_session < 0:
            return False, "auto_approve_max_per_session must be a non-negative integer"

        if self.auto_approve_max_per_session > 100:
            return False, "auto_approve_max_per_session too large (max: 100)"

        if not isinstance(self.auto_approve_bash, bool):
            return False, "auto_approve_bash must be a boolean"

        if not isinstance(self.auto_approve_tools, list):
            return False, "auto_approve_tools must be a list"
        for tool in self.auto_approve_tools:
            if not isinstance(tool, str) or not tool.strip():
                return False, "auto_approve_tools contains an empty tool name"
            if len(tool.strip()) > 80:
                return False, "auto_approve_tools contains a tool name that is too long"

        if not isinstance(self.continuation_prompt, str) or not self.continuation_prompt.strip():
            return False, "continuation_prompt cannot be empty"

        if len(self.continuation_prompt) > 8000:
            return False, "continuation_prompt is too long (max: 8000 characters)"

        for field_name, patterns in (
            ("incomplete_patterns", self.incomplete_patterns),
            ("blocker_patterns", self.blocker_patterns),
        ):
            if not isinstance(patterns, list):
                return False, f"{field_name} must be a list"
            if len(patterns) > 128:
                return False, f"{field_name} contains too many patterns (max: 128)"
            for pattern in patterns:
                if not isinstance(pattern, str) or not pattern.strip():
                    return False, f"{field_name} contains an empty pattern"
                if len(pattern) > 512:
                    return False, f"{field_name} contains a pattern that is too long (max: 512 characters)"

        for pattern in self.incomplete_patterns:
            try:
                re.compile(pattern)
            except re.error as e:
                return False, f"Invalid incomplete pattern '{pattern}': {e}"

        for pattern in self.blocker_patterns:
            try:
                re.compile(pattern)
            except re.error as e:
                return False, f"Invalid blocker pattern '{pattern}': {e}"

        return True, ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AutoContinueSettings":
        """Create settings from a dict with default-pattern migration."""
        if not isinstance(data, dict):
            data = {}
        known_fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        defaults = cls()
        for field_name in BOOL_SETTING_FIELDS:
            if field_name in known_fields:
                known_fields[field_name] = _coerce_bool_setting(
                    known_fields[field_name],
                    getattr(defaults, field_name),
                )
        for field_name in STRING_SETTING_FIELDS:
            if field_name in known_fields:
                known_fields[field_name] = _coerce_string_setting(
                    known_fields[field_name],
                    getattr(defaults, field_name),
                )
        for field_name in INTEGER_SETTING_FIELDS:
            if field_name in known_fields:
                known_fields[field_name] = _coerce_int_setting(
                    known_fields[field_name],
                    getattr(defaults, field_name),
                )
        if "training_prompt_template_key" in known_fields:
            known_fields["training_prompt_template_key"] = (
                _resolve_training_prompt_template_key(known_fields["training_prompt_template_key"])
                or DEFAULT_TRAINING_PROMPT_TEMPLATE_KEY
            )
        if "incomplete_patterns" in known_fields:
            known_fields["incomplete_patterns"] = _merge_unique_patterns(
                known_fields.get("incomplete_patterns"),
                DEFAULT_INCOMPLETE_PATTERNS,
            )
        if "blocker_patterns" in known_fields:
            known_fields["blocker_patterns"] = _merge_unique_patterns(
                known_fields.get("blocker_patterns"),
                DEFAULT_BLOCKER_PATTERNS,
            )
        if "auto_approve_tools" in known_fields:
            known_fields["auto_approve_tools"] = _merge_unique_strings(
                known_fields.get("auto_approve_tools"),
                [],
            )
            if known_fields.get("auto_approve_bash", True) and known_fields["auto_approve_tools"]:
                known_fields["auto_approve_tools"] = _merge_unique_strings(
                    ["Bash"],
                    known_fields["auto_approve_tools"],
                )
        elif known_fields.get("auto_approve_bash") is False:
            known_fields["auto_approve_tools"] = [
                tool for tool in DEFAULT_PERMISSION_AUTO_APPROVE_TOOLS
                if tool.casefold() != "bash"
            ]

        instance = cls(**known_fields)
        is_valid, error = instance.validate()
        if not is_valid:
            raise ValueError(f"Invalid settings: {error}")
        return instance


@dataclass
class ProviderStatus:
    """Status of a provider's auto-continue installation."""

    provider_name: str
    enabled: bool = False
    hook_script_exists: bool = False
    hook_registered: bool = False
    guidance_installed: bool = False
    error_recovery_installed: bool = False
    last_error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)
