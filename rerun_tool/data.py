import csv  # 导入 CSV 解析工具。 
from dataclasses import dataclass  # 导入数据类装饰器。 
from enum import Enum  # 导入枚举类型工具。 
from typing import List, Optional  # 导入类型注解工具。 


class WorkflowKind(Enum):  # 定义工作流类型枚举。 
    VERIFY_PATCH = 'verify_patch'  # 表示带补丁的验证流程。 
    DETECT_FLAKY = 'detect_flaky'  # 表示不打补丁的 flaky 检测流程。 


class RunnerBackend(Enum):  # 定义测试执行后端枚举。 
    STANDARD = 'standard'  # 表示普通 surefire 或 gradle test 重跑。 
    NONDEX = 'nondex'  # 表示使用 NonDex 进行重跑。 


@dataclass  # 定义旧版补丁数据条目以保持兼容。 
class TestEntry:  # 当前仓库已有逻辑和测试仍然依赖这个结构。 
    index: int  # 保存原始 CSV 行号。 
    repo_url: str  # 保存仓库地址。 
    repo_owner: str  # 保存仓库 owner。 
    project_name: str  # 保存项目名。 
    original_sha: str  # 保存待验证提交号。 
    fixed_sha: str  # 保存修复提交号。 
    module: str  # 保存模块名。 
    full_test_name: str  # 保存完整测试名。 
    pr_link: str  # 保存 PR 链接。 
    flaky_code: str  # 保存原始 flaky 方法文本。 
    fixed_code: str  # 保存人工修复代码文本。 
    diff: str  # 保存 diff 文本。 
    generated_patch: str  # 保存待应用补丁文本。 
    is_correct: str  # 保存标签字段。 
    source_file: str  # 保存源文件路径。 
    rerun_consistency: str = ''  # 保存原始输入里的 rerun_consistency 字段。 

    @property  # 计算测试类全名。 
    def test_class(self) -> str:  # 从 full_test_name 提取测试类名。 
        parts = self.full_test_name.rsplit('.', 2)  # 先按可能重复的方法名格式做拆分。 
        if len(parts) >= 3 and parts[1] == parts[2]:  # 数据集里有一类格式会重复两次方法名。 
            return parts[0]  # 这种情况下第一段就是完整类名。 
        return self.full_test_name.rsplit('.', 1)[0]  # 其余情况下取最后一个点之前的全部内容。 

    @property  # 计算测试方法名。 
    def test_method(self) -> str:  # 从 full_test_name 提取测试方法。 
        parts = self.full_test_name.rsplit('.', 2)  # 先按可能重复的方法名格式做拆分。 
        if len(parts) >= 3 and parts[1] == parts[2]:  # 处理方法名被重复写入的数据格式。 
            return parts[1]  # 这种情况下第二段就是方法名。 
        return self.full_test_name.rsplit('.', 1)[-1]  # 否则取最后一个点之后的内容。 

    @property  # 计算简单类名。 
    def simple_class_name(self) -> str:  # 去掉包名后返回类名。 
        return self.test_class.rsplit('.', 1)[-1]  # 只保留最后一段类名。 

    @property  # 计算类路径。 
    def class_path(self) -> str:  # 将类名转换成 Java 文件路径。 
        return self.test_class.replace('.', '/') + '.java'  # 按 Java 包路径规则拼接源码相对路径。 

    @property  # 计算兼容旧逻辑的唯一标识。 
    def unique_id(self) -> str:  # 返回当前测试条目的稳定标识。 
        return f"{self.project_name}_{self.simple_class_name}_{self.test_method}_{self.index}"  # 用项目、类、方法和行号构造唯一键。 

    @property  # 暴露旧数据条目的默认工作流名称。 
    def workflow_name(self) -> str:  # 旧版 TestEntry 总是补丁验证流程。 
        return WorkflowKind.VERIFY_PATCH.value  # 返回补丁验证工作流枚举值。 

    @property  # 暴露旧数据条目的默认执行后端。 
    def runner_backend_name(self) -> str:  # 旧版 TestEntry 默认走标准重跑后端。 
        return RunnerBackend.STANDARD.value  # 返回标准执行后端枚举值。 

    @property  # 暴露旧数据条目的输入来源。 
    def input_source(self) -> str:  # 旧版 TestEntry 来自 patch CSV。 
        return 'patch_csv'  # 返回 patch CSV 来源标签。 

    @property  # 暴露旧数据条目的补丁模式。 
    def patch_mode(self) -> str:  # 旧版 TestEntry 天然带补丁。 
        return 'with_patch'  # 返回带补丁模式标签。 

    @property  # 兼容结果写出层对原始 rerun consistency 字段的统一读取。 
    def original_rerun_consistency(self) -> str:  # 返回旧版输入中的 rerun consistency 标签。 
        return self.rerun_consistency  # 旧版 TestEntry 直接复用原字段值。 

    @property  # 为结果恢复与 resume 暴露稳定请求键。 
    def request_key(self) -> str:  # 使用工作流、后端和目标测试拼出稳定主键。 
        return f"{self.workflow_name}:{self.runner_backend_name}:{self.original_sha}:{self.unique_id}"  # 返回旧版兼容请求键。 


@dataclass  # 定义与工作流无关的测试目标。 
class TestTarget:  # 该结构只描述“跑哪个仓库里的哪个测试”。 
    index: int  # 保存当前输入源中的顺序号。 
    repo_url: str  # 保存仓库地址。 
    repo_owner: str  # 保存仓库 owner。 
    project_name: str  # 保存项目名。 
    original_sha: str  # 保存待检验提交号。 
    fixed_sha: str = ''  # 保存可选的修复提交号。 
    module: str = '.'  # 保存模块名，默认表示仓库根模块。 
    full_test_name: str = ''  # 保存完整测试名。 
    source_file: str = ''  # 保存可选源文件路径。 
    input_source: str = 'csv'  # 保存当前输入来源标签。 

    @property  # 计算测试类全名。 
    def test_class(self) -> str:  # 复用旧数据格式的解析规则。 
        parts = self.full_test_name.rsplit('.', 2)  # 先按可能重复的方法名格式做拆分。 
        if len(parts) >= 3 and parts[1] == parts[2]:  # 处理方法名被重复写入的场景。 
            return parts[0]  # 第一段即为完整类名。 
        return self.full_test_name.rsplit('.', 1)[0]  # 其余情况下取最后一个点之前的内容。 

    @property  # 计算测试方法名。 
    def test_method(self) -> str:  # 复用旧数据格式的解析规则。 
        parts = self.full_test_name.rsplit('.', 2)  # 先按可能重复的方法名格式做拆分。 
        if len(parts) >= 3 and parts[1] == parts[2]:  # 处理方法名重复场景。 
            return parts[1]  # 第二段即为方法名。 
        return self.full_test_name.rsplit('.', 1)[-1]  # 否则取最后一个点后的内容。 

    @property  # 计算简单类名。 
    def simple_class_name(self) -> str:  # 去掉包名后返回类名。 
        return self.test_class.rsplit('.', 1)[-1]  # 只保留类名本身。 

    @property  # 计算类路径。 
    def class_path(self) -> str:  # 将类名转换成 Java 文件路径。 
        return self.test_class.replace('.', '/') + '.java'  # 返回源码相对路径。 

    @property  # 计算目标级唯一标识。 
    def unique_id(self) -> str:  # 返回目标测试的稳定标识。 
        return f"{self.project_name}_{self.simple_class_name}_{self.test_method}_{self.index}"  # 用项目、类、方法和行号构造目标键。 


@dataclass  # 定义补丁相关载荷。 
class PatchSpec:  # 该结构只描述补丁文本与元数据。 
    pr_link: str = ''  # 保存 PR 链接。 
    flaky_code: str = ''  # 保存原始 flaky 方法文本。 
    fixed_code: str = ''  # 保存人工修复代码文本。 
    diff: str = ''  # 保存 diff 文本。 
    generated_patch: str = ''  # 保存生成补丁文本。 
    is_correct: str = ''  # 保存标签字段。 
    original_rerun_consistency: str = ''  # 保存原始输入里的 rerun_consistency 字段。 

    @property  # 判断补丁是否真正存在。 
    def has_patch(self) -> bool:  # 通过 generated_patch 是否非空判断。 
        return bool(self.generated_patch.strip())  # 返回补丁是否存在。 


@dataclass  # 定义统一运行请求。 
class RunRequest:  # 新架构下所有输入最终都会被转换成这个结构。 
    target: TestTarget  # 保存测试目标。 
    workflow: WorkflowKind = WorkflowKind.VERIFY_PATCH  # 保存工作流类型。 
    runner_backend: RunnerBackend = RunnerBackend.STANDARD  # 保存执行后端类型。 
    patch: Optional[PatchSpec] = None  # 保存可选补丁载荷。 

    @property  # 暴露索引以兼容旧逻辑。 
    def index(self) -> int:  # 返回目标索引。 
        return self.target.index  # 透传目标索引。 

    @property  # 暴露仓库地址以兼容旧逻辑。 
    def repo_url(self) -> str:  # 返回仓库地址。 
        return self.target.repo_url  # 透传目标仓库地址。 

    @property  # 暴露仓库 owner 以兼容旧逻辑。 
    def repo_owner(self) -> str:  # 返回仓库 owner。 
        return self.target.repo_owner  # 透传目标仓库 owner。 

    @property  # 暴露项目名以兼容旧逻辑。 
    def project_name(self) -> str:  # 返回项目名。 
        return self.target.project_name  # 透传目标项目名。 

    @property  # 暴露原始提交号以兼容旧逻辑。 
    def original_sha(self) -> str:  # 返回原始提交号。 
        return self.target.original_sha  # 透传目标提交号。 

    @property  # 暴露修复提交号以兼容旧逻辑。 
    def fixed_sha(self) -> str:  # 返回修复提交号。 
        return self.target.fixed_sha  # 透传修复提交号。 

    @property  # 暴露模块名以兼容旧逻辑。 
    def module(self) -> str:  # 返回模块名。 
        return self.target.module  # 透传目标模块名。 

    @property  # 暴露完整测试名以兼容旧逻辑。 
    def full_test_name(self) -> str:  # 返回完整测试名。 
        return self.target.full_test_name  # 透传完整测试名。 

    @property  # 暴露测试类名以兼容旧逻辑。 
    def test_class(self) -> str:  # 返回测试类全名。 
        return self.target.test_class  # 透传目标测试类名。 

    @property  # 暴露测试方法名以兼容旧逻辑。 
    def test_method(self) -> str:  # 返回测试方法名。 
        return self.target.test_method  # 透传目标测试方法名。 

    @property  # 暴露简单类名以兼容旧逻辑。 
    def simple_class_name(self) -> str:  # 返回简单类名。 
        return self.target.simple_class_name  # 透传简单类名。 

    @property  # 暴露类路径以兼容旧逻辑。 
    def class_path(self) -> str:  # 返回类路径。 
        return self.target.class_path  # 透传类路径。 

    @property  # 暴露唯一目标键以兼容旧逻辑。 
    def unique_id(self) -> str:  # 返回目标唯一键。 
        return self.target.unique_id  # 透传目标唯一键。 

    @property  # 暴露源文件路径以兼容旧逻辑。 
    def source_file(self) -> str:  # 返回源文件路径。 
        return self.target.source_file  # 透传源文件路径。 

    @property  # 暴露 PR 链接以兼容旧逻辑。 
    def pr_link(self) -> str:  # 返回补丁 PR 链接。 
        return self.patch.pr_link if self.patch else ''  # 无补丁时返回空串。 

    @property  # 暴露 flaky 代码以兼容旧逻辑。 
    def flaky_code(self) -> str:  # 返回原始 flaky 方法文本。 
        return self.patch.flaky_code if self.patch else ''  # 无补丁时返回空串。 

    @property  # 暴露 fixed_code 以兼容旧逻辑。 
    def fixed_code(self) -> str:  # 返回人工修复代码文本。 
        return self.patch.fixed_code if self.patch else ''  # 无补丁时返回空串。 

    @property  # 暴露 diff 以兼容旧逻辑。 
    def diff(self) -> str:  # 返回 diff 文本。 
        return self.patch.diff if self.patch else ''  # 无补丁时返回空串。 

    @property  # 暴露 generated_patch 以兼容旧逻辑。 
    def generated_patch(self) -> str:  # 返回生成补丁文本。 
        return self.patch.generated_patch if self.patch else ''  # 无补丁时返回空串。 

    @property  # 暴露标签字段以兼容旧逻辑。 
    def is_correct(self) -> str:  # 返回标签字段。 
        return self.patch.is_correct if self.patch else ''  # 无补丁时返回空串。 

    @property  # 暴露原始 rerun_consistency 字段给结果层使用。 
    def original_rerun_consistency(self) -> str:  # 返回原始输入中的 rerun consistency 标签。 
        return self.patch.original_rerun_consistency if self.patch else ''  # 无补丁时返回空串。 

    @property  # 暴露工作流名称给结果层使用。 
    def workflow_name(self) -> str:  # 返回当前工作流枚举值。 
        return self.workflow.value  # 将工作流枚举转换为字符串。 

    @property  # 暴露执行后端名称给结果层使用。 
    def runner_backend_name(self) -> str:  # 返回当前执行后端枚举值。 
        return self.runner_backend.value  # 将执行后端枚举转换为字符串。 

    @property  # 暴露输入来源给结果层使用。 
    def input_source(self) -> str:  # 返回目标输入来源。 
        return self.target.input_source  # 透传目标输入来源。 

    @property  # 暴露补丁模式给结果层使用。 
    def patch_mode(self) -> str:  # 根据 patch 是否存在返回补丁模式。 
        return 'with_patch' if self.patch and self.patch.has_patch else 'no_patch'  # 返回补丁模式标签。 

    @property  # 暴露恢复主键给结果层使用。 
    def request_key(self) -> str:  # 使用工作流、后端、提交号和目标键构造恢复主键。 
        return f"{self.workflow_name}:{self.runner_backend_name}:{self.original_sha}:{self.unique_id}"  # 返回稳定请求键。 


def load_csv(csv_path: str, rows: Optional[List[int]] = None, limit: Optional[int] = None) -> List[TestEntry]:  # 保留旧版补丁 CSV 加载函数以兼容现有逻辑。 
    entries: List[TestEntry] = []  # 初始化返回列表。 
    with open(csv_path, 'r', encoding='utf-8') as f:  # 以 UTF-8 方式读取 CSV。 
        reader = csv.DictReader(f)  # 用字典读取器解析表头。 
        for i, row in enumerate(reader):  # 按顺序遍历每一行输入。 
            if not row.get('repo_url', '').strip():  # 缺失仓库地址的行无法执行。 
                continue  # 跳过无效行。 
            if not row.get('generated_patch', '').strip():  # 旧版加载函数仍然只接受带补丁的数据。 
                continue  # 跳过无补丁行以保持旧接口兼容。 
            if rows is not None and i not in rows:  # 如果指定了行号过滤则只保留命中项。 
                continue  # 跳过不在目标集合中的行。 
            repo_owner, project_name = _resolve_repo_identity(row.get('repo_url', '').strip(), row.get('repo_owner', '').strip(), row.get('project_name', '').strip())  # 统一补齐仓库身份字段。 
            entries.append(TestEntry(  # 构造旧版补丁条目对象。 
                index=i,  # 写入当前行号。 
                repo_url=row['repo_url'].strip(),  # 写入仓库地址。 
                repo_owner=repo_owner,  # 写入仓库 owner。 
                project_name=project_name,  # 写入项目名。 
                original_sha=row.get('original_sha', '').strip(),  # 写入原始提交号。 
                fixed_sha=row.get('fixed_sha', '').strip(),  # 写入修复提交号。 
                module=_normalize_module(row.get('module', '').strip()),  # 写入模块名并统一默认值。 
                full_test_name=row.get('full_test_name', '').strip(),  # 写入完整测试名。 
                pr_link=row.get('pr_link', '').strip(),  # 写入 PR 链接。 
                flaky_code=row.get('flaky_code', '').strip(),  # 写入 flaky 代码文本。 
                fixed_code=row.get('fixed_code', '').strip(),  # 写入 fixed_code 文本。 
                diff=row.get('diff', '').strip(),  # 写入 diff 文本。 
                generated_patch=row.get('generated_patch', '').strip(),  # 写入生成补丁文本。 
                is_correct=row.get('isCorrect', '').strip(),  # 写入标签字段。 
                source_file=row.get('source_file', '').strip(),  # 写入源文件路径。 
                rerun_consistency=row.get('rerun_consistency', '').strip(),  # 写入原始输入中的 rerun consistency 字段。 
            ))  # 完成旧版补丁条目构造。 
            if limit is not None and len(entries) >= limit:  # 达到上限后提前结束。 
                break  # 停止继续读取 CSV。 
    return entries  # 返回旧版条目列表。 


def load_patch_requests(csv_path: str, rows: Optional[List[int]] = None, limit: Optional[int] = None, runner_backend: RunnerBackend = RunnerBackend.STANDARD) -> List[RunRequest]:  # 加载补丁验证请求。 
    return [request_from_test_entry(entry, runner_backend=runner_backend) for entry in load_csv(csv_path, rows=rows, limit=limit)]  # 先走旧版加载器再转换成统一请求。 


def load_flaky_requests(csv_path: str, rows: Optional[List[int]] = None, limit: Optional[int] = None, runner_backend: RunnerBackend = RunnerBackend.STANDARD) -> List[RunRequest]:  # 加载 patchless flaky 检测请求。 
    requests: List[RunRequest] = []  # 初始化请求列表。 
    with open(csv_path, 'r', encoding='utf-8') as f:  # 以 UTF-8 方式读取输入 CSV。 
        reader = csv.DictReader(f)  # 使用字典读取器解析表头。 
        for i, row in enumerate(reader):  # 逐行遍历输入。 
            if rows is not None and i not in rows:  # 如果指定了行号则只保留目标行。 
                continue  # 跳过当前行。 
            repo_url = row.get('repo_url', '').strip()  # 读取仓库地址。 
            full_test_name = row.get('full_test_name', '').strip()  # 读取完整测试名。 
            original_sha = row.get('original_sha', '').strip()  # 读取目标提交号。 
            if not repo_url or not full_test_name or not original_sha:  # 这三个字段是 patchless 检测的最小输入集合。 
                continue  # 缺失关键字段时跳过当前行。 
            repo_owner, project_name = _resolve_repo_identity(repo_url, row.get('repo_owner', '').strip(), row.get('project_name', '').strip())  # 补齐仓库身份字段。 
            target = TestTarget(  # 构造测试目标对象。 
                index=i,  # 写入当前行号。 
                repo_url=repo_url,  # 写入仓库地址。 
                repo_owner=repo_owner,  # 写入仓库 owner。 
                project_name=project_name,  # 写入项目名。 
                original_sha=original_sha,  # 写入目标提交号。 
                fixed_sha=row.get('fixed_sha', '').strip(),  # 写入可选修复提交号。 
                module=_normalize_module(row.get('module', '').strip()),  # 写入模块名并统一默认值。 
                full_test_name=full_test_name,  # 写入完整测试名。 
                source_file=row.get('source_file', '').strip(),  # 写入可选源文件路径。 
                input_source='flaky_csv',  # 标记当前输入来源为 patchless CSV。 
            )  # 完成测试目标构造。 
            requests.append(RunRequest(target=target, workflow=WorkflowKind.DETECT_FLAKY, runner_backend=runner_backend, patch=None))  # 构造统一检测请求。 
            if limit is not None and len(requests) >= limit:  # 达到上限后提前结束。 
                break  # 停止继续读取 CSV。 
    return requests  # 返回 patchless 检测请求列表。 


def build_cli_request(repo_url: str, original_sha: str, full_test_name: str, module: str = '.', repo_owner: str = '', project_name: str = '', source_file: str = '', runner_backend: RunnerBackend = RunnerBackend.STANDARD) -> RunRequest:  # 根据单条 CLI 参数构造检测请求。 
    resolved_owner, resolved_project = _resolve_repo_identity(repo_url, repo_owner.strip(), project_name.strip())  # 统一补齐仓库身份字段。 
    target = TestTarget(  # 构造单条测试目标对象。 
        index=0,  # 单条 CLI 模式固定使用 0 作为输入索引。 
        repo_url=repo_url.strip(),  # 写入仓库地址。 
        repo_owner=resolved_owner,  # 写入仓库 owner。 
        project_name=resolved_project,  # 写入项目名。 
        original_sha=original_sha.strip(),  # 写入目标提交号。 
        fixed_sha='',  # 单条 CLI 检测模式不需要修复提交号。 
        module=_normalize_module(module.strip()),  # 写入模块名并统一默认值。 
        full_test_name=full_test_name.strip(),  # 写入完整测试名。 
        source_file=source_file.strip(),  # 写入可选源文件路径。 
        input_source='cli',  # 标记当前输入来源为单条 CLI。 
    )  # 完成单条测试目标构造。 
    return RunRequest(target=target, workflow=WorkflowKind.DETECT_FLAKY, runner_backend=runner_backend, patch=None)  # 返回 patchless 检测请求。 


def request_from_test_entry(entry: TestEntry, runner_backend: RunnerBackend = RunnerBackend.STANDARD) -> RunRequest:  # 将旧版 TestEntry 转换为统一运行请求。 
    target = TestTarget(  # 先构造目标对象。 
        index=entry.index,  # 透传原始索引。 
        repo_url=entry.repo_url,  # 透传仓库地址。 
        repo_owner=entry.repo_owner,  # 透传仓库 owner。 
        project_name=entry.project_name,  # 透传项目名。 
        original_sha=entry.original_sha,  # 透传目标提交号。 
        fixed_sha=entry.fixed_sha,  # 透传修复提交号。 
        module=_normalize_module(entry.module),  # 透传模块名并统一默认值。 
        full_test_name=entry.full_test_name,  # 透传完整测试名。 
        source_file=entry.source_file,  # 透传源文件路径。 
        input_source='patch_csv',  # 标记当前输入来源为补丁 CSV。 
    )  # 完成目标对象构造。 
    patch = PatchSpec(  # 再构造补丁对象。 
        pr_link=entry.pr_link,  # 透传 PR 链接。 
        flaky_code=entry.flaky_code,  # 透传 flaky 方法文本。 
        fixed_code=entry.fixed_code,  # 透传人工修复代码文本。 
        diff=entry.diff,  # 透传 diff 文本。 
        generated_patch=entry.generated_patch,  # 透传生成补丁文本。 
        is_correct=entry.is_correct,  # 透传标签字段。 
        original_rerun_consistency=entry.rerun_consistency,  # 透传原始输入中的 rerun consistency 字段。 
    )  # 完成补丁对象构造。 
    return RunRequest(target=target, workflow=WorkflowKind.VERIFY_PATCH, runner_backend=runner_backend, patch=patch)  # 返回统一补丁验证请求。 


def _normalize_module(module: str) -> str:  # 统一模块字段的默认表示。 
    return module if module and module != '' else '.'  # 空字符串模块统一视为仓库根模块。 


def _resolve_repo_identity(repo_url: str, repo_owner: str, project_name: str) -> tuple[str, str]:  # 从显式字段或 repo_url 中补齐 owner 与项目名。 
    normalized_url = repo_url.rstrip('/')  # 先去掉末尾斜杠便于解析。 
    if normalized_url.endswith('.git'):  # 去掉常见的 .git 后缀便于提取项目名。 
        normalized_url = normalized_url[:-4]  # 删除末尾 .git。 
    url_parts = [part for part in normalized_url.split('/') if part]  # 将 URL 按路径分段。 
    inferred_project = project_name or (url_parts[-1] if url_parts else '')  # 优先使用显式项目名，否则从 URL 末段推断。 
    inferred_owner = repo_owner or (url_parts[-2] if len(url_parts) >= 2 else '')  # 优先使用显式 owner，否则从 URL 倒数第二段推断。 
    return inferred_owner, inferred_project  # 返回补齐后的 owner 与项目名。 
