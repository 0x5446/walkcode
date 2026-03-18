# AskUserQuestion Feishu 自动化测试文档

## 概述

本文档说明 AskUserQuestion Feishu 交互卡片功能的自动化回归测试框架。

## 测试结构

### 测试文件
- **路径**: `tests/test_askuserquestion_feishu.py`
- **覆盖范围**: 单问题、多问题、边界条件、错误处理、性能基准

### 测试类

#### 1. `TestAskUserQuestionCard` - 卡片生成单元测试
- `test_single_question_card_structure()`: 验证单问题卡片结构正确
- `test_multi_question_progress_indicator()`: 验证多问题进度指示器 (1/2), (2/2)

#### 2. `TestAskUserQuestionIntegration` - 集成测试
- `test_single_question_workflow()`: 单问题完整工作流
- `test_multi_question_workflow()`: 多问题顺序处理工作流
- `test_multi_question_answer_format()`: 多问题答案格式验证（数组）
- `test_single_question_answer_format()`: 单问题答案格式验证（字符串）

**辅助方法**:
```python
send_permission_request(questions)  # 发送权限请求，返回 request_id
get_decision(request_id, timeout)   # 获取决定，检查状态
```

#### 3. `TestAskUserQuestionEdgeCases` - 边界情况
- `test_empty_options_list()`: 无选项的问题
- `test_single_option_question()`: 单选项问题
- `test_many_options()`: 多个选项（20+）
- `test_special_characters_in_labels()`: 特殊字符和 Unicode（中文、emoji）

#### 4. `TestAskUserQuestionErrorHandling` - 错误处理
- `test_invalid_request_id()`: 无效 request_id 返回 not_found
- `test_expired_request()`: 请求过期清理

#### 5. `TestAskUserQuestionPerformance` - 性能基准
- `test_card_generation_performance()`: 卡片生成 < 100ms
- `test_answer_processing_performance()`: 答案处理响应快速

## 运行测试

### 方式 1: 手动运行

```bash
# 运行所有测试
python scripts/run_tests.py

# 运行特定标记的测试
python scripts/run_tests.py -m integration
python scripts/run_tests.py -m edge_case

# 详细输出
python scripts/run_tests.py -v

# 生成报告
python scripts/run_tests.py --report
```

### 方式 2: Git 预提交自动触发

每次提交时自动运行回归测试（已通过 `.git/hooks/pre-commit` 配置）：

```bash
git commit -m "fix: update askuserquestion handler"
# 自动运行回归测试，如果失败则中止提交

# 跳过测试（如果需要）
git commit --no-verify
```

### 方式 3: GitHub Actions CI/CD

推送到 `main` 或 `develop` 分支时自动运行：
- 文件 `.github/workflows/regression-tests.yml` 定义工作流
- 相关文件变更时触发：`server.py`, `test_*.py`, `scripts/run_tests.py`
- PR 上自动添加测试结果评论

## 测试服务器要求

所有测试需要 walkcode 服务器在 `http://localhost:3001` 运行。

### 自动启动
运行脚本时如果服务器未运行，会自动启动：
```bash
python scripts/run_tests.py  # 自动检查并启动服务器
```

### 手动启动
```bash
# 开发模式运行本地代码
python -m walkcode serve
```

### 检查服务器状态
```bash
curl -s http://localhost:3001/health || echo "Server not running"
```

## 测试数据流

### 单问题流程
```
问题: {"question": "...", "options": [...]}
    ↓
send_permission_request() → request_id
    ↓
用户点击选项 (模拟)
    ↓
GET /hook/permission/{request_id}/decision
    ↓
返回: {"status": "decided", "decision": {"behavior": "allow", "answer": "value"}}
```

### 多问题流程
```
问题数组: [Q1, Q2, Q3]
    ↓
send_permission_request() → request_id
    ↓
显示 Q1 (progress: 1/3) → 用户点击
    ↓
显示 Q2 (progress: 2/3) → 用户点击
    ↓
显示 Q3 (progress: 3/3) → 用户点击
    ↓
返回: {"behavior": "allow", "answers": ["ans1", "ans2", "ans3"]}
```

## 验证检查清单

每次修改 AskUserQuestion 相关代码后，确保：

- [ ] 单问题卡片显示正确
- [ ] 多问题顺序处理工作正常
- [ ] 按钮点击返回正确的答案值
- [ ] 答案格式符合预期（单问题是字符串，多问题是数组）
- [ ] 特殊字符和 Unicode 正确处理
- [ ] 边界情况处理正确（空列表、单选项等）
- [ ] 无效 request_id 返回 not_found
- [ ] 性能指标达标

## 测试报告

### 本地报告
运行 `python scripts/run_tests.py --report` 后生成：
- `test_results.json`: 详细 JSON 格式报告
- 包含: 总数、通过、失败、跳过、耗时

### GitHub Actions 报告
- PR 上自动评论显示测试结果
- Artifacts 保存 30 天的测试报告

## 继续完善计划

### 近期（下一周）
- [ ] 添加更多特殊字符测试用例
- [ ] 添加网络延迟模拟测试
- [ ] 添加大量数据压力测试（100+ 选项）

### 中期（下个月）
- [ ] 添加性能基准跟踪
- [ ] 添加覆盖率报告
- [ ] 集成到发布流程检查

### 长期（持续）
- [ ] 根据实际用户反馈添加新测试
- [ ] 优化测试执行时间
- [ ] 扩展到其他工具的权限测试

## 故障排除

### 问题: 服务器启动失败

```bash
# 1. 检查端口占用
lsof -i :3001

# 2. 清理僵尸进程
lsof -i :3001 -t | xargs kill -9

# 3. 手动启动验证
python -m walkcode serve
```

### 问题: 测试连接失败

```bash
# 1. 检查网络连接
curl http://localhost:3001/health

# 2. 检查防火墙
sudo lsof -i :3001

# 3. 查看服务器日志
tail -f ~/.walkcode/logs/server.log
```

### 问题: git 钩子不执行

```bash
# 1. 检查钩子权限
ls -la .git/hooks/pre-commit

# 2. 重新设置权限
chmod +x .git/hooks/pre-commit

# 3. 检查钩子内容
cat .git/hooks/pre-commit
```

## 相关资源

- [AskUserQuestion 实现详情](memory/askuserquestion_feishu_implementation.md)
- [权限系统文档](memory/permission_system_fix.md)
- [Feishu 卡片容器支持](memory/feishu_card_containers.md)
