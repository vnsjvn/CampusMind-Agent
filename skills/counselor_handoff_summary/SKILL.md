---
name: counselor_handoff_summary
description: Use when creating a staff-facing handoff summary for counselors or administrators after a psychological risk report is generated.
version: 1.0.0
enabled: true
priority: 900
---

# Counselor Handoff Summary

## Workflow

- Write for counselors or administrators, not for the student.
- Preserve the student's original meaning while avoiding unnecessary dangerous detail.
- Include the report identity, student identity, risk level, emotion label, confidence, model summary, follow-up actions, and a bounded excerpt of the student's expression.
- Make the first follow-up action about current location, whether the student is accompanied, and immediate safety.
- Keep the handoff factual and actionable; do not add diagnosis or unsupported assumptions.

## Output Template

```text
应用 skill: counselor_handoff_summary
报告ID：{{report_id}}
学生：{{student}}
风险等级：{{risk_level}}
情绪标签：{{emotion}}
置信度：{{confidence}}
模型摘要：{{summary}}
建议跟进：
{{next_steps}}
学生原始表达：
{{content_excerpt}}
```
