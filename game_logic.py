# game_logic.py
def calculate_grade(total_bills):
    """根据金额返回模糊分级"""
    #if total_bills <= 5: return "少"
    if total_bills <= 10: return "较少"
    if total_bills <= 20: return "一般"
    #if total_bills <= 20: return "较多"
    return "较多"

def validate_defense(rule_id, boxes, user_balance):
    """
    校验防守部署是否合法
    boxes: [{'c10': int, 'c100': int}, ...]
    """
    # 1. 基础数值校验
    for b in boxes:
        if b['c10'] < 0 or b['c100'] < 0:
            return False, "代币数量不能为负数"
        if b['c10'] == 0 and b['c100'] == 0:
            return False, "每个盒子至少需要一张代币"

    amounts = [b['c10'] * 10 + b['c100'] * 100 for b in boxes]
    counts_10 = [b['c10'] for b in boxes]
    counts_total = [b['c10'] + b['c100'] for b in boxes]
    
    total_amount = sum(amounts)
    
    if total_amount < 3000: return False, "部署总金额不能少于 3,000"
    if total_amount > user_balance: return False, "余额不足"

    # 2. 规则校验
    if rule_id == 1: # 规则 1：等差序列 (金额)
        sorted_amounts = sorted(amounts)
        diff = sorted_amounts[1] - sorted_amounts[0]
        # 简单校验：需容错浮点，但此处是整数，直接判断
        for i in range(1, 22):
            if sorted_amounts[i] - sorted_amounts[i-1] != diff: 
                return False, "金额不构成等差数列"
                
    elif rule_id == 2: # 规则 2：特异点
        target_count = counts_total[0]
        if not all(c == target_count for c in counts_total): 
            return False, "所有盒子代币张数必须相同"
        
        pure_10_boxes = 0
        pure_100_boxes = 0
        
        for b in boxes:
            if b['c10'] > 0 and b['c100'] == 0:
                pure_10_boxes += 1
            elif b['c100'] > 0 and b['c10'] == 0:
                pure_100_boxes += 1
            else:
                return False, "规则2要求盒子内只能有一种面值的代币"
        
        if pure_100_boxes != 1:
            return False, "必须有且仅有 1 个盒子装入100元面值"
        if pure_10_boxes != 21:
            return False, "必须有 21 个盒子只装入10元面值"

    elif rule_id == 3: # 规则 3：囚犯困局
        c10_set = set(counts_10)
        if len(c10_set) != 22:
            return False, "10元面值张数必须严格对应 1~22 张且不重复"
        if min(counts_10) != 1 or max(counts_10) != 22:
            return False, "10元面值张数必须在 1~22 之间"
            
    else: 
        return False, "未知规则类型"

    return True, "验证通过"