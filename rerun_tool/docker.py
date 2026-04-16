"""Docker-based execution environment for Java projects.

Detects the required JDK version from project configuration and runs
build/test commands inside appropriate Docker containers.

Uses Docker named volumes (not bind mounts) for Maven/Gradle caches
to avoid the severe macOS Docker Desktop I/O performance penalty.
"""

import logging
import os
import re
import subprocess
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

# Mapping from detected Java version to Docker image
JDK_IMAGE_MAP = {
    '1.5': 'maven:3.8.6-openjdk-8',
    '1.6': 'maven:3.8.6-openjdk-8',
    '1.7': 'maven:3.8.6-openjdk-8',
    '1.8': 'maven:3.8.6-openjdk-8',
    '8': 'maven:3.8.6-openjdk-8',
    '9': 'maven:3.8.6-openjdk-11',
    '10': 'maven:3.8.6-openjdk-11',
    '11': 'maven:3.8.6-openjdk-11',
    '12': 'maven:3.8.6-openjdk-11',
    '13': 'maven:3.8.6-openjdk-11',
    '14': 'maven:3.8.6-openjdk-11',
    '15': 'maven:3.8.6-openjdk-11',
    '16': 'maven:3.8.6-openjdk-18',
    '17': 'maven:3.8.6-openjdk-18',
    '18': 'maven:3.8.6-openjdk-18',
    '19': 'maven:3.9-eclipse-temurin-21',
    '20': 'maven:3.9-eclipse-temurin-21',
    '21': 'maven:3.9-eclipse-temurin-21',
}
DEFAULT_IMAGE = 'maven:3.8.6-openjdk-11'

GRADLE_JDK_IMAGE_MAP = {
    '1.5': 'gradle:7.6-jdk8',
    '1.6': 'gradle:7.6-jdk8',
    '1.7': 'gradle:7.6-jdk8',
    '1.8': 'gradle:7.6-jdk8',
    '8': 'gradle:7.6-jdk8',
    '11': 'gradle:7.6-jdk11',
    '17': 'gradle:7.6-jdk17',
    '21': 'gradle:8.5-jdk21',
}
DEFAULT_GRADLE_IMAGE = 'gradle:7.6-jdk11'

# Docker named volume for Maven/Gradle caches (much faster than bind mounts on macOS)
MAVEN_CACHE_VOLUME = 'rerun-tool-m2-repo'
GRADLE_CACHE_VOLUME = 'rerun-tool-gradle-cache'
GRADLE_WRAPPER_VOLUME = 'rerun-tool-gradle-wrapper'  # 为 Gradle wrapper 单独提供缓存卷以减少重复下载。


def detect_java_version(repo_dir: str, module: str = '') -> str:  # 支持按模块检测 Java 版本以减少多模块误判。
    """Detect the required Java version from project configuration."""
    for search_dir in _candidate_project_dirs(repo_dir, module):  # 先查模块再回退到父目录和仓库根目录。
        pom_path = os.path.join(search_dir, 'pom.xml')  # 优先读取 Maven 配置。
        if os.path.isfile(pom_path):  # 如果当前目录存在 pom 则尝试检测。
            detected = _detect_from_pom(pom_path)  # 解析当前 pom 中声明或属性引用的版本。
            if detected:  # 一旦命中明确版本就立即返回。
                return detected  # 返回当前目录最接近实际构建单元的版本。
        for gradle_file in ['build.gradle', 'build.gradle.kts']:  # 同时支持两种 Gradle 构建脚本。
            gradle_path = os.path.join(search_dir, gradle_file)  # 拼出 Gradle 配置文件路径。
            if os.path.isfile(gradle_path):  # 如果存在 Gradle 配置则尝试解析。
                detected = _detect_from_gradle(gradle_path)  # 从 Gradle 配置中读取语言级别。
                if detected:  # 命中有效版本时立即返回。
                    return detected  # 返回最靠近模块的 Gradle 版本声明。
    return ''  # 所有候选目录均未识别时返回空串。


def _candidate_project_dirs(repo_dir: str, module: str) -> list:  # 为模块化项目生成由近到远的搜索目录列表。
    repo_root = os.path.abspath(repo_dir)  # 统一为绝对路径以便后续比较。
    candidate_dirs = []  # 保存待搜索的目录顺序。
    if module and module != '.':  # 只有声明了具体模块时才做模块向上回溯。
        current_dir = os.path.abspath(os.path.join(repo_root, module))  # 先从模块目录本身开始搜索。
        while current_dir.startswith(repo_root):  # 仅在仓库目录内部逐层向上回退。
            if current_dir not in candidate_dirs:  # 避免因为路径规范化导致重复目录。
                candidate_dirs.append(current_dir)  # 记录当前候选目录。
            if current_dir == repo_root:  # 到达仓库根目录后结束。
                break  # 停止继续向上搜索。
            parent_dir = os.path.dirname(current_dir)  # 获取当前目录的父目录。
            if parent_dir == current_dir:  # 理论上的安全保护，防止死循环。
                break  # 如果路径无法继续上移则退出。
            current_dir = parent_dir  # 继续沿着模块父目录链向上查找。
    if repo_root not in candidate_dirs:  # 保证仓库根目录至少被检查一次。
        candidate_dirs.append(repo_root)  # 将仓库根目录追加为最后兜底候选。
    return candidate_dirs  # 返回有序候选目录列表。


def _detect_from_pom(pom_path: str, seen_paths: Optional[set] = None) -> str:  # 支持沿着 Maven parent 链继续查找 Java 版本。
    normalized_pom_path = os.path.abspath(pom_path)  # 先将 pom 路径规范化以便做循环检测。
    if seen_paths is None:  # 第一次进入递归时初始化已访问集合。
        seen_paths = set()  # 使用集合记录已经解析过的 pom 路径。
    if normalized_pom_path in seen_paths:  # 如果当前 pom 已被访问过则停止递归。
        return ''  # 避免 parent 链异常时出现无限递归。
    seen_paths.add(normalized_pom_path)  # 记录当前 pom 已经开始解析。
    try:
        with open(normalized_pom_path, 'r', encoding='utf-8', errors='ignore') as f:  # 以宽松模式读取 pom 文本。
            content = f.read()  # 读取完整 pom 内容以便做属性解析。
        properties = _extract_pom_properties(content)  # 先提取 properties 中的键值定义。
        patterns = [
            r'<maven\.compiler\.source>([^<]+)</maven\.compiler\.source>',  # 支持属性占位符而不仅是纯数字。
            r'<maven\.compiler\.target>([^<]+)</maven\.compiler\.target>',  # 支持 target 通过属性间接声明。
            r'<maven\.compiler\.release>([^<]+)</maven\.compiler\.release>',  # release 是新版本 Maven 常见写法。
            r'<java\.version>([^<]+)</java\.version>',  # 常见项目级 Java 版本属性。
            r'<jdk\.version>([^<]+)</jdk\.version>',  # 一些项目会使用自定义 jdk.version。
            r'<javac\.source>([^<]+)</javac\.source>',  # 兼容旧项目的 javac.source 写法。
            r'<java\.source\.version>([^<]+)</java\.source\.version>',  # 兼容其他自定义属性名。
        ]
        for pattern in patterns:  # 依次检查常见的 pom 版本声明位置。
            match = re.search(pattern, content)  # 搜索当前模式是否在 pom 中出现。
            if match:  # 命中后尝试解析字面量或属性引用。
                resolved = _resolve_property_value(match.group(1).strip(), properties)  # 展开诸如 ${java.version} 的占位符。
                normalized = _normalize_java_version(resolved)  # 统一为镜像映射可识别的版本字符串。
                if normalized:  # 只有解析出明确版本时才返回。
                    return normalized  # 返回规范化后的 Java 版本。
        source_match = re.search(r'<source>([^<]+)</source>', content)  # 兼容编译插件内联的 source 字段。
        if source_match:  # 如果声明了 source 则同样尝试解析。
            resolved = _resolve_property_value(source_match.group(1).strip(), properties)  # 解析可能存在的属性引用。
            normalized = _normalize_java_version(resolved)  # 将结果规整成统一格式。
            if normalized:  # 只在解析成功时返回。
                return normalized  # 返回最终的 source 版本。
        parent_pom_path = _resolve_parent_pom_path(normalized_pom_path, content)  # 如果当前 pom 未声明 Java 版本则继续沿 parent 链查找。
        if parent_pom_path and os.path.isfile(parent_pom_path):  # 只有本地 parent pom 存在时才递归解析。
            inherited = _detect_from_pom(parent_pom_path, seen_paths)  # 递归读取 parent pom 中的 Java 版本信息。
            if inherited:  # 一旦在 parent 链上找到版本就立即返回。
                return inherited  # 返回从 parent pom 继承到的 Java 版本。
    except Exception:
        pass
    return ''


def _resolve_parent_pom_path(pom_path: str, content: str) -> Optional[str]:  # 根据 <parent> 和 <relativePath> 解析本地 parent pom 路径。
    parent_block_match = re.search(r'<parent>(.*?)</parent>', content, re.DOTALL)  # 尝试提取 parent 配置块。
    if not parent_block_match:  # 没有 parent 块时无需继续解析。
        return None  # 返回空值表示不存在本地父 pom。
    parent_block = parent_block_match.group(1)  # 取出 parent 块内部文本。
    relative_path_match = re.search(r'<relativePath>(.*?)</relativePath>', parent_block, re.DOTALL)  # 查找显式声明的 relativePath。
    if relative_path_match is None:  # Maven 未显式声明 relativePath 时使用默认规则。
        relative_path = '../pom.xml'  # Maven 的默认本地父 pom 路径是上一层目录的 pom.xml。
    else:  # 存在显式 relativePath 时按其内容处理。
        relative_path = relative_path_match.group(1).strip()  # 读取并去掉首尾空白。
    if not relative_path:  # 空 relativePath 代表显式禁用本地 parent 查找。
        return None  # 此时不能再按本地文件递归解析。
    resolved_path = os.path.abspath(os.path.join(os.path.dirname(pom_path), relative_path))  # 基于当前 pom 目录拼出本地 parent 路径。
    if os.path.isdir(resolved_path):  # 一些项目的 relativePath 指向的是父模块目录而不是具体 pom 文件。
        resolved_path = os.path.join(resolved_path, 'pom.xml')  # 遇到目录路径时自动补全到该目录下的 pom.xml。
    return resolved_path  # 返回最终解析出的本地 parent pom 绝对路径。


def _extract_pom_properties(content: str) -> dict:  # 从 pom 的 properties 块中提取键值对。
    properties = {}  # 初始化属性字典。
    properties_block_match = re.search(r'<properties>(.*?)</properties>', content, re.DOTALL)  # 尝试获取整个 properties 块。
    if not properties_block_match:  # 没有 properties 块时直接返回空字典。
        return properties  # 无属性可供展开。
    properties_block = properties_block_match.group(1)  # 取出 properties 的内部文本。
    for key, value in re.findall(r'<([A-Za-z0-9_.-]+)>(.*?)</\1>', properties_block, re.DOTALL):  # 提取简单键值标签。
        properties[key.strip()] = value.strip()  # 去掉多余空白后写入字典。
    return properties  # 返回可用于占位符展开的属性集合。


def _resolve_property_value(value: str, properties: dict, depth: int = 0) -> str:  # 递归展开 Maven 属性引用。
    if not value:  # 空值无需继续处理。
        return ''  # 直接返回空串。
    if depth > 5:  # 递归层数过深通常意味着循环引用。
        return value  # 直接返回当前值以避免无限递归。
    property_match = re.fullmatch(r'\$\{([^}]+)\}', value)  # 仅处理完整占位符形式的值。
    if not property_match:  # 字面量值无需展开。
        return value  # 直接返回原值。
    property_name = property_match.group(1).strip()  # 提取属性名。
    if property_name not in properties:  # 未知属性无法进一步解析。
        return value  # 保留原始占位符便于后续兜底逻辑处理。
    return _resolve_property_value(properties[property_name], properties, depth + 1)  # 递归展开下一层属性值。


def _normalize_java_version(value: str) -> str:  # 将不同形式的 Java 版本规范化为镜像映射使用的格式。
    if not value:  # 空值无法规范化。
        return ''  # 返回空串表示未知版本。
    stripped = value.strip()  # 先移除首尾空白。
    if not stripped:  # 全空白同样视为未知版本。
        return ''  # 返回空串。
    if stripped.startswith('VERSION_'):  # 兼容 JavaVersion.VERSION_17 这类格式。
        stripped = stripped.replace('VERSION_', '').replace('_', '.')  # 转成普通数字版本形式。
    match = re.search(r'(\d+(?:\.\d+)?)', stripped)  # 从复杂字符串中提取第一个版本号片段。
    if not match:  # 提取不到数字时无法识别。
        return ''  # 返回空串给上层兜底。
    return match.group(1)  # 返回识别到的主版本或 1.x 版本表示。


def _detect_from_gradle(gradle_path: str) -> str:
    try:
        with open(gradle_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        patterns = [
            r'sourceCompatibility\s*=\s*[\'"]?([\d.]+)',
            r'targetCompatibility\s*=\s*[\'"]?([\d.]+)',
            r'JavaVersion\.VERSION_([\d_]+)',
            r'languageVersion\.set\(JavaLanguageVersion\.of\((\d+)\)\)',
        ]
        for pattern in patterns:
            match = re.search(pattern, content)
            if match:
                return match.group(1).replace('_', '.')
    except Exception:
        pass
    return ''


def check_local_jdk(required_version: str) -> bool:
    """Check if the local JDK is compatible with the required version.

    Returns True if local JDK can compile source at the required level.
    """
    if not required_version:
        return True  # No specific requirement, assume local works

    try:
        local_major = _detect_local_jdk_major()  # 优先读取实际构建工具报告的 JDK 版本而不是裸 java 版本。
        if local_major is None:  # 如果完全无法识别本地构建 JDK，则视为不兼容。
            return False
        # Normalize required version
        req = required_version.replace('1.', '') if required_version.startswith('1.') else required_version
        req_major = int(req.split('.')[0])

        # Local JDK must be >= required version
        if req_major < 8 and local_major > 8:  # 现代 JDK 往往无法编译 source/target 5/6/7 这类过老级别。
            return False  # 对老源码级别强制判为不兼容，转交 Docker 中的旧 JDK 处理。
        return local_major >= req_major  # 其余情况沿用主版本比较即可。
    except Exception:
        return False


def _detect_local_jdk_major() -> Optional[int]:  # 检测本地实际构建链会使用的 JDK 主版本。
    version_commands = [  # 按照“实际构建工具优先”的顺序检测 JDK 版本。
        ['mvn', '-version'],  # Maven 输出最能代表本工具当前的本地构建环境。
        ['gradle', '-version'],  # 对 Gradle 项目则尽量读取 Gradle 自己报告的 JVM。
        ['java', '-version'],  # 最后才回退到裸 java 版本。
    ]  # 保持顺序以避免被系统默认 java 误导。
    for cmd in version_commands:  # 依次尝试所有版本检测命令。
        try:  # 单个命令失败时继续尝试下一个。
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)  # 执行版本探测命令。
        except Exception:  # 命令不存在或执行失败时忽略即可。
            continue  # 尝试下一种探测方式。
        version_output = result.stderr + result.stdout  # 合并标准输出和错误输出统一解析。
        parsed_major = _parse_java_major(version_output)  # 从工具输出中提取 JDK 主版本。
        if parsed_major is not None:  # 一旦解析成功就直接返回。
            logger.debug(f"Detected local build JDK {parsed_major} from: {' '.join(cmd)}")  # 记录实际命中的版本来源。
            return parsed_major  # 返回最贴近真实构建环境的主版本。
    return None  # 所有探测方式都失败时返回空值。


def _parse_java_major(version_output: str) -> Optional[int]:  # 从不同工具的版本输出中提取统一的 JDK 主版本。
    patterns = [  # 兼容 Maven、Gradle 和 java 命令的常见输出格式。
        r'Java version:\s*"?([0-9]+)(?:\.([0-9]+))?',  # 匹配 Maven 的 Java version 行。
        r'JVM:\s*"?([0-9]+)(?:\.([0-9]+))?',  # 匹配 Gradle 的 JVM 行。
        r'version "(\d+)(?:\.(\d+))?',  # 匹配 java -version 的标准输出。
    ]  # 每个模式都返回主版本和可选次版本。
    for pattern in patterns:  # 逐个尝试不同输出格式。
        match = re.search(pattern, version_output)  # 在当前输出中搜索版本号。
        if not match:  # 当前模式未命中时继续。
            continue  # 尝试下一个模式。
        major = int(match.group(1))  # 提取匹配到的首段数字。
        return major if major > 1 else int(match.group(2) or '0')  # 兼容 1.8 这类旧格式。
    return None  # 所有模式都未命中时返回空值。


def get_docker_image(repo_dir: str, build_tool: str = 'maven', module: str = '') -> str:  # 支持根据模块配置选择更准确的镜像。
    """Get the appropriate Docker image for the project."""
    java_version = detect_java_version(repo_dir, module)  # 先按模块检测，再回退到仓库级配置。
    if build_tool == 'gradle':
        image_map = GRADLE_JDK_IMAGE_MAP
        default = DEFAULT_GRADLE_IMAGE
    else:
        image_map = JDK_IMAGE_MAP
        default = DEFAULT_IMAGE
    overridden_version = _project_specific_java_override(repo_dir, build_tool, java_version)  # 对少数已验证的编译器边界项目应用更稳定的 JDK 覆盖。
    effective_version = overridden_version or java_version  # 优先使用项目特定覆盖，否则沿用常规版本检测结果。

    if effective_version:
        image = image_map.get(effective_version, default)
        if overridden_version:  # 单独记录项目覆盖，避免误以为是普通版本探测结果。
            logger.info(f"Java version {java_version or 'unknown'} overridden to {effective_version} -> Docker image {image}")
        else:
            logger.info(f"Java version {effective_version} -> Docker image {image}")
    else:
        image = default
        logger.info(f"Java version not detected, using default: {image}")
    return image


def _project_specific_java_override(repo_dir: str, build_tool: str, java_version: str) -> str:  # 为少数已验证存在编译器边界问题的项目返回更稳定的 JDK 版本。
    if build_tool != 'maven':  # 当前覆盖仅针对 Maven 项目。
        return ''  # Gradle 项目保持常规版本检测逻辑。
    if java_version not in {'1.5', '1.6', '1.7', '1.8', '8'}:  # 只在旧源码级别项目里考虑编译器兼容性覆盖。
        return ''  # 现代 Java 版本项目无需特殊处理。
    root_pom = os.path.join(os.path.abspath(repo_dir), 'pom.xml')  # 仅基于仓库根 pom 做项目签名判断。
    if not os.path.isfile(root_pom):  # 根 pom 不存在时无法安全识别项目。
        return ''  # 保持默认镜像选择。
    try:  # 根 pom 读取失败时直接回退到默认逻辑。
        with open(root_pom, 'r', encoding='utf-8', errors='ignore') as f:  # 宽松读取 pom 文本。
            pom_text = f.read()  # 读取完整 pom 内容做签名判断。
    except Exception:
        return ''  # 读取异常时不启用覆盖。
    json_java_markers = (  # 该组合用于稳定识别当前失败集中唯一确认需要避开 JDK8 编译器边界的项目。
        '<name>JSON in Java</name>',
        'douglascrockford/JSON-java',
        '<source>1.6</source>',
        '<target>1.6</target>',
    )  # 只有全部命中时才执行覆盖，避免把其他老项目误伤到 JDK11。
    if all(marker in pom_text for marker in json_java_markers):  # `JSON-java` 在 JDK11 下可编译，而 JDK8 容易触发参考补丁编译边界问题。
        return '11'  # 将该项目固定提升到 JDK11 镜像以提高 Docker 复现稳定性。
    return ''  # 其余项目不启用覆盖。


def pull_image(image: str) -> bool:
    """Pull a Docker image if not already present."""
    result = subprocess.run(
        ['docker', 'image', 'inspect', image],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return True

    logger.info(f"Pulling Docker image: {image}")
    result = subprocess.run(
        ['docker', 'pull', image],
        capture_output=True, text=True, timeout=600
    )
    if result.returncode != 0:
        logger.error(f"Failed to pull image {image}: {result.stderr}")
        return False
    return True


def _ensure_volumes():
    """Create Docker named volumes for caches if they don't exist."""
    for vol in [MAVEN_CACHE_VOLUME, GRADLE_CACHE_VOLUME, GRADLE_WRAPPER_VOLUME]:  # 同时缓存 Gradle 依赖和 wrapper 分发包。
        subprocess.run(
            ['docker', 'volume', 'create', vol],
            capture_output=True, text=True, timeout=10
        )


def is_docker_available() -> bool:
    """Check if Docker daemon is running and responsive."""
    try:
        result = subprocess.run(
            ['docker', 'info', '--format', '{{.ServerVersion}}'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            return True
    except Exception:
        pass
    return False


def docker_run(image: str, repo_dir: str, cmd: list,
               timeout: int = 600, extra_env: dict = None) -> subprocess.CompletedProcess:
    """Run a command inside a Docker container.

    Uses a two-phase approach for reliability on macOS:
    1. docker create (create container)
    2. docker start -a (attach and run)
    This avoids the macOS Docker Desktop hang that can occur with docker run.

    Uses Docker named volumes for Maven/Gradle caches (fast on macOS).

    Args:
        image: Docker image to use.
        repo_dir: Repository directory (mounted at /workspace).
        cmd: Command to run inside container.
        timeout: Timeout in seconds.
        extra_env: Additional environment variables.

    Returns:
        CompletedProcess result.
    """
    _ensure_volumes()

    container_name = f"rerun-tool-{uuid.uuid4().hex[:8]}"

    # Phase 1: Create container
    create_cmd = [
        'docker', 'create',
        '--name', container_name,
        '-v', f'{os.path.abspath(repo_dir)}:/workspace',
        '-v', f'{MAVEN_CACHE_VOLUME}:/root/.m2/repository',
        '-v', f'{GRADLE_CACHE_VOLUME}:/root/.gradle/caches',  # 挂载 Gradle 依赖缓存。
        '-v', f'{GRADLE_WRAPPER_VOLUME}:/root/.gradle/wrapper',  # 挂载 Gradle wrapper 缓存以减少首次下载波动。
        '-w', '/workspace',
        '--memory=4g',
        '--cpus=2',
    ]

    env_vars = {'CI': 'true', 'MAVEN_OPTS': '-Xmx2g -Xms512m'}
    if extra_env:
        env_vars.update(extra_env)
    for k, v in env_vars.items():
        create_cmd.extend(['-e', f'{k}={v}'])

    create_cmd.append(image)
    create_cmd.extend(cmd)

    logger.debug(f"Docker create: {container_name} | {' '.join(cmd)}")

    try:
        create_result = subprocess.run(
            create_cmd, capture_output=True, text=True, timeout=30
        )
        if create_result.returncode != 0:
            logger.error(f"Docker create failed: {create_result.stderr}")
            return create_result

        # Phase 2: Start container and attach (wait for output)
        logger.debug(f"Docker start: {container_name}")
        start_result = subprocess.run(
            ['docker', 'start', '-a', container_name],
            capture_output=True, text=True, timeout=timeout
        )

        return start_result

    except subprocess.TimeoutExpired:
        logger.warning(f"Docker timed out after {timeout}s, killing {container_name}")
        subprocess.run(['docker', 'kill', container_name],
                       capture_output=True, text=True, timeout=10)
        raise
    finally:
        # Always clean up the container
        subprocess.run(
            ['docker', 'rm', '-f', container_name],
            capture_output=True, text=True, timeout=10
        )


def should_use_docker(repo_dir: str, module: str = '') -> bool:  # 支持按模块判断是否需要 Docker。
    """Decide whether Docker is needed based on local JDK compatibility.

    Returns True if Docker is needed (local JDK incompatible).
    """
    java_version = detect_java_version(repo_dir, module)  # 基于最相关模块配置检测 Java 版本。
    if not java_version:  # 无法确定版本时转向更稳定的容器环境。
        if is_docker_available():  # 如果 Docker 可用则优先容器以提升可复现性。
            logger.info("Java version not detected, preferring Docker for reproducibility")  # 记录选择 Docker 的原因。
            return True  # 未知环境下优先容器执行。
        logger.info("Java version not detected and Docker unavailable, falling back to local execution")  # 没有 Docker 时只能本地执行。
        return False  # 在无 Docker 条件下保留本地回退能力。

    if check_local_jdk(java_version):
        logger.info(f"Local JDK compatible with Java {java_version}, using local execution")
        return False

    logger.info(f"Local JDK incompatible with Java {java_version}, Docker required")
    return True
