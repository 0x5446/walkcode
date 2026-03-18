# 🧪 AskUserQuestion 自动化测试 - 快速开始

## 🎯 目标

为 AskUserQuestion Feishu 卡片功能建立完整的自动化回归测试框架，确保每次代码变更都经过验证。

## ✅ 已完成的工作

### 1. 测试套件 (`tests/test_askuserquestion_feishu.py`)
- ✅ **5 个测试类**，15+ 个测试方法
- ✅ 单问题工作流验证
- ✅ 多问题顺序处理验证
- ✅ 卡片格式和进度指示验证
- ✅ 边界情况处理（空列表、Unicode、特殊字符）
- ✅ 错误处理（无效 ID、过期请求）
- ✅ 性能基准验证

### 2. 测试运行器 (`scripts/run_tests.py`)
- ✅ 自动检查/启动 walkcode 服务器
- ✅ 执行 pytest 并收集结果
- ✅ 生成 JSON 格式的测试报告
- ✅ 友好的控制台输出摘要

### 3. Git 预提交钩子 (`.git/hooks/pre-commit`)
- ✅ 配置完成并可执行
- ✅ 每次 `git commit` 前自动运行测试
- ✅ 测试失败时中止提交（可用 `--no-verify` 跳过）

### 4. GitHub Actions CI/CD (`.github/workflows/regression-tests.yml`)
- ✅ 推送到 main/develop 分支时自动运行
- ✅ PR 上自动评论显示测试结果
- ✅ 30 天的测试报告保留期

### 5. 文档
- ✅ `TESTING.md` - 完整的测试文档
- ✅ Memory 系统 - 记录完整的实现细节

## 🚀 使用方法

### 方式 1: 手动运行测试

```bash
# 运行所有测试
python scripts/run_tests.py

# 运行特定类别
python scripts/run_tests.py -m integration
python scripts/run_tests.py -m edge_case

# 详细输出
python scripts/run_tests.py -v

# 生成报告
python scripts/run_tests.py --report
```

### 方式 2: Git 自动触发（推荐）

```bash
# 正常提交，钩子会自动运行测试
git add .
git commit -m "fix: update AskUserQuestion handler"
# → 自动运行测试，如果失败则中止提交

# 如需跳过（紧急情况）
git commit --no-verify
```

### 方式 3: GitHub Actions（自动）

推送到 GitHub 时自动运行：
```bash
git push origin main
# → GitHub 自动在 CI 环境中运行完整测试
# → PR 上显示 ✅/❌ 评论
```

## 📋 测试覆盖范围

### 单问题工作流
```
用户选择一个选项 → 返回: {"behavior": "allow", "answer": "value"}
```

### 多问题工作流
```
Q1 (1/3) → 选择 → Q2 (2/3) → 选择 → Q3 (3/3) → 选择
→ 返回: {"behavior": "allow", "answers": ["a1", "a2", "a3"]}
```

### 边界情况
- ✅ 空选项列表
- ✅ 单个选项
- ✅ 20+ 个选项
- ✅ 中文 + emoji（选择语言 🐍🎯🦀）
- ✅ 其他 Unicode 字符

### 错误处理
- ✅ 无效 request_id → not_found
- ✅ 过期请求 → 清理验证

### 性能
- ✅ 卡片生成 < 100ms
- ✅ 答案处理响应快速

## 🔧 系统要求

### 自动处理
- walkcode 服务器（如未运行，脚本自动启动）
- Python 3.11+
- 依赖：pytest, requests, walkcode

### 依赖安装
```bash
pip install pytest pytest-json-report requests

# 如果还未安装 walkcode
pip install -e .
```

## 📊 查看测试报告

### 本地
```bash
python scripts/run_tests.py --report
# 生成: test_results.json
cat test_results.json
```

### GitHub Actions
1. 推送到 GitHub
2. 在 PR 上查看自动评论（显示通过/失败）
3. 在 Actions 标签页查看详细日志
4. 从 Artifacts 下载 test_results.json

## 🐛 故障排查

### 问题: 服务器启动失败

```bash
# 清理占用端口的进程
lsof -i :3001 -t | xargs kill -9

# 手动启动验证
python -m walkcode serve
```

### 问题: 测试连接失败

```bash
# 检查服务器
curl http://localhost:3001/health

# 如果输出错误，查看日志
tail -f ~/.walkcode/logs/server.log
```

### 问题: 钩子不执行

```bash
# 重新设置权限
chmod +x .git/hooks/pre-commit

# 验证钩子
cat .git/hooks/pre-commit
```

## 📈 继续完善

### 下一步改进（已规划）
- [ ] 添加更多特殊字符和语言测试
- [ ] 网络延迟和超时模拟
- [ ] 大数据量压力测试（100+ 选项）
- [ ] 性能基准跟踪和图表
- [ ] 代码覆盖率报告

### 长期改进
- [ ] 根据用户反馈添加测试
- [ ] 优化测试执行时间
- [ ] 集成到发布检查清单
- [ ] 扩展到其他权限工具

## 📚 相关文档

- **完整测试文档**: [TESTING.md](TESTING.md)
- **测试框架详解**: [memory/testing_framework.md](memory/testing_framework.md)
- **AskUserQuestion 实现**: [memory/askuserquestion_feishu_implementation.md](memory/askuserquestion_feishu_implementation.md)
- **权限系统**: [memory/permission_system_fix.md](memory/permission_system_fix.md)

## ✨ 核心特性

| 特性 | 说明 |
|------|------|
| **自动启动** | 测试运行器自动启动 walkcode 服务器 |
| **本地钩子** | 每次提交前自动验证，防止回归 |
| **CI/CD** | GitHub 自动运行完整测试套件 |
| **PR 反馈** | 自动在 PR 上显示测试结果 |
| **报告生成** | JSON 格式报告便于集成 |
| **易于调试** | 详细日志和错误消息 |

---

**现在可以开始使用了！** 🎉

每次修改 AskUserQuestion 相关代码后，系统会自动验证功能完整性。
