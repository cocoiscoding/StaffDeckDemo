修复规则：repair_context 表示上一次步骤结果未满足结构或槽位约束。重新检查当前用户输入、对话历史、slots 和 repair_context 中明确指出的缺失项，只补充有事实依据的 slot_updates 或动作，不得为通过校验而编造数据。
