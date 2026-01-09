import time, random, uuid
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room
from game_logic import validate_defense, calculate_grade

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret_key_haff_arena'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet', ping_timeout=60)

rooms = {} 
online_users = {} 
reset_timers = {} # 用于存储房间自动重置的定时器ID

def get_lobby_data():
    return [{"id": rid, "owner": r['owner'], "count": len(r['players']), "state": r['state']} for rid, r in rooms.items()]

@app.route('/')
def index(): return render_template('index.html')

@socketio.on('connect')
def on_connect(): pass

@socketio.on('disconnect')
def on_disconnect():
    if request.sid in online_users: del online_users[request.sid]

@socketio.on('enter_lobby')
def on_enter(data):
    uid = data.get('userId')
    if uid:
        online_users[request.sid] = uid
        emit('lobby_update', get_lobby_data())

@socketio.on('reconnect_user')
def on_reconnect(data):
    uid = data.get('userId')
    if not uid: return
    online_users[request.sid] = uid 
    found_room = None
    for rid, room in rooms.items():
        if uid in room["players"]:
            found_room = room
            break     
    if found_room:
        rid = found_room["id"]
        join_room(rid) 
        is_spectator = (found_room["state"] == "GAME" and uid not in found_room["players"])
        emit('join_success', {**found_room, "is_spectator": is_spectator, "is_reconnect": True})
        if found_room["state"] == "GAME":
            socketio.sleep(0.2)
            broadcast_game_state(rid, target_uid=uid)
            emit('reconnect_result', {'success': True, 'msg': f'已重连至房间 {rid}'})
        else:
            emit('room_sync', found_room)
            emit('reconnect_result', {'success': True, 'msg': '已回到准备大厅'})
    else:
        emit('reconnect_result', {'success': True, 'msg': '欢迎回来'})
        emit('lobby_update', get_lobby_data())

@socketio.on('create_room')
def on_create(data):
    rid, uid = data.get('roomId'), data.get('userId')
    if rid in rooms: return emit('error', {'msg': '房间已存在'})
    rooms[rid] = {
        "id": rid, "players": [uid], "ready": {uid: False}, "owner": uid, 
        "state": "LOBBY", "scores": {uid: 10000}, "matches": {}, "bye_player": None,
        "chat_history": [],
        "history": [], # 游戏历史记录
        "summary_confirms": [] # 统计确认人数 (List)
    }
    join_room(rid)
    emit('join_success', rooms[rid])
    socketio.emit('lobby_update', get_lobby_data())

@socketio.on('join_room')
def on_join(data):
    rid, uid = data.get('roomId'), data.get('userId')
    if rid not in rooms: return emit('error', {'msg': '房间不存在'})
    room = rooms[rid]
    join_room(rid)
    is_player = uid in room["players"]
    if room["state"] == "GAME" and not is_player:
        emit('join_success', {**room, "is_spectator": True})
        broadcast_game_state(rid, target_uid=uid) 
        return
    if not is_player:
        if len(room["players"]) >= 10: return emit('error', {'msg': '房间满员'})
        room["players"].append(uid)
        room["ready"][uid] = False
        room["scores"][uid] = 10000
    emit('join_success', {**room, "is_spectator": False})
    socketio.emit('lobby_update', get_lobby_data())
    emit('room_sync', room, room=rid)

@socketio.on('leave_room')
def on_leave(data):
    rid, uid = data.get('roomId'), data.get('userId')
    if rid in rooms and uid in rooms[rid]["players"]:
        room = rooms[rid]
        if room["state"] == "LOBBY":
            room["players"].remove(uid)
            del room["ready"][uid]
            del room["scores"][uid]
            leave_room(rid)
            if len(room["players"]) == 0: del rooms[rid]
            elif uid == room["owner"]: room["owner"] = room["players"][0]
            socketio.emit('lobby_update', get_lobby_data())
            if rid in rooms: emit('room_sync', room, room=rid)
            emit('leave_success')
        else:
            emit('leave_success')

@socketio.on('set_ready')
def on_ready(data):
    rid, uid = data.get('roomId'), data.get('userId')
    if rid in rooms and uid in rooms[rid]["ready"]:
        rooms[rid]["ready"][uid] = not rooms[rid]["ready"][uid]
        emit('room_sync', rooms[rid], room=rid)

@socketio.on('send_chat')
def on_chat(data):
    rid, uid, msg = data.get('roomId'), data.get('userId'), data.get('msg')
    if rid in rooms:
        payload = {'user': uid, 'msg': msg, 'type': 'chat'}
        rooms[rid]['chat_history'].append(payload)
        socketio.emit('chat_message', payload, room=rid)

# --- 游戏核心逻辑 ---

def round_timer_task(rid, match_id, round_num):
    socketio.sleep(300) 
    room = rooms.get(rid)
    if not room: return
    match = room["matches"].get(match_id)
    if not match: return
    
    # 检查是否仍在同一轮且处于布防阶段
    if match["round"] == round_num and match["game_data"]["step"] == "SETUP":
        defender = match["defender"]
        attacker = match["attacker"]
        penalty = 3000
        
        # 修正：直接操作分数并传递给历史记录
        room["scores"][defender] -= penalty
        room["scores"][attacker] += penalty
        
        msg = f"⏱️ 防守方超时未部署！扣除 {penalty} 转移给攻方。"
        socketio.emit('chat_message', {'user': '裁判', 'msg': msg, 'type': 'info'}, room=rid)
        
        finish_round(rid, match, reason="DEF_TIMEOUT", penalty_data={
            'def_delta': -penalty, 'atk_delta': penalty
        })

@socketio.on('start_game')
def on_start(data):
    rid = data.get('roomId')
    room = rooms.get(rid)
    if not room or not all(room["ready"].values()) or len(room["players"]) < 2:
        return emit('error', {'msg': '需满2人且全员准备'})
    room["state"] = "GAME"
    players = room["players"][:]
    random.shuffle(players)
    room["matches"] = {}
    room["history"] = [] # 重置历史
    room["bye_player"] = players.pop() if len(players) % 2 != 0 else None
    
    for i in range(0, len(players), 2):
        match_id = str(uuid.uuid4())[:8]
        current_time = time.time()
        room["matches"][match_id] = {
            "id": match_id, "p1": players[i], "p2": players[i+1],
            "defender": players[i], "attacker": players[i+1], "round": 1,
            "round_history": [], 
            "game_data": { "step": "SETUP", "boxes": [], "rule": 0, "strategy": 0, "deadline": current_time + 300 }
        }
        socketio.start_background_task(round_timer_task, rid, match_id, 1)
    socketio.emit('lobby_update', get_lobby_data())
    broadcast_game_state(rid)

def broadcast_game_state(rid, target_uid=None):
    room = rooms.get(rid)
    if not room: return
    match_list = [{"id": m["id"], "p1": m["p1"], "p2": m["p2"], "round": m["round"], "step": m["game_data"]["step"]} for m in room["matches"].values()]
    common_data = {"match_list": match_list, "scores": room["scores"]}
    targets = [target_uid] if target_uid else room["players"]
    for p in targets:
        sid = next((k for k,v in online_users.items() if v == p), None)
        if not sid: continue
        my_match = None
        role_info = {"role": "spectator", "match_id": None}
        if p == room["bye_player"]: 
            role_info["is_bye"] = True
        else:
            for m in room["matches"].values():
                if p == m["p1"] or p == m["p2"]:
                    my_match = m
                    role_info = {"role": "defender" if p == m["defender"] else "attacker", "match_id": m["id"]}
                    break
        deadline = my_match["game_data"].get("deadline", 0) if my_match else 0
        socketio.emit('game_update', {
            **common_data, 
            "role_info": role_info, 
            "match_data": my_match,
            "server_time": time.time(),
            "round_deadline": deadline
        }, room=sid)

@socketio.on('lock_rule')
def on_lock_rule(data):
    match = get_match(data['roomId'], data['userId'])
    if match and match["defender"] == data['userId']:
        match["game_data"]["rule"] = int(data['rule'])
        broadcast_game_state(data['roomId'])

@socketio.on('submit_defense')
def on_submit_def(data):
    rid, uid = data['roomId'], data['userId']
    room = rooms[rid]
    match = get_match(rid, uid)
    if not match or match["defender"] != uid: return
    boxes = data['boxes']
    rule = match["game_data"]["rule"]
    if rule == 0: return emit('error', {'msg': '请先选择规则'})
    valid, msg = validate_defense(rule, boxes, room["scores"][uid])
    if not valid: return emit('error', {'msg': msg})
    total_cost = sum([b['c10']*10 + b['c100']*100 for b in boxes])
    
    room["scores"][uid] -= total_cost
    match["game_data"]["boxes"] = boxes
    match["game_data"]["step"] = "ATTACK_SELECT" 
    public_boxes = []
    for i, b in enumerate(boxes):
        public_boxes.append({
            "id": i, "grade": calculate_grade(b['c10']+b['c100']),
            "revealed": False, "real_c10": 0, "real_c100": 0, "taken": False
        })
    match["game_data"]["public_boxes"] = public_boxes
    broadcast_game_state(rid)

@socketio.on('select_strategy')
def on_select_strat(data):
    rid, uid = data['roomId'], data['userId']
    match = get_match(rid, uid)
    if not match or match["attacker"] != uid: return
    strat = int(data['strategy'])
    rule = match["game_data"]["rule"]
    valid_strat = False
    if strat == 1: valid_strat = True
    elif strat == 2 and rule == 1: valid_strat = True
    elif strat == 3 and rule == 2: valid_strat = True
    elif strat == 4 and rule == 3: valid_strat = True
    if not valid_strat: return emit('error', {'msg': '该策略不可用于当前防守规则'})
    
    gd = match["game_data"]
    gd["strategy"] = strat
    gd["step"] = "ATTACKING"
    gd["attempts"] = 10 if strat in [1,2] else 0
    
    # 策略初始化
    if strat == 3: 
        gd["guesses"] = 0
    elif strat == 4:
        # 方案D初始化
        gd["s4"] = {
            "stage": 0,         # 0:攻方定数, 1:攻方揭示, 2:守方定数, 3:守方揭示, 4:最终选择
            "target_x": 0,      # 攻方寻找的目标
            "target_y": 0,      # 守方指定的目标
            "revealed_phase1": [], 
            "revealed_phase2": [], 
            "wins": 0,          
            "start_time": time.time() 
        }
    
    broadcast_game_state(rid)

# --- 方案D: 博弈逻辑 ---

def s4_check_timeout(match, room_id):
    s4 = match["game_data"].get("s4")
    if not s4: return False
    if time.time() - s4["start_time"] > 180:
        msg = "⏳ 攻方思考超时（3分钟）！本轮结束，所有资金归守方。"
        socketio.emit('chat_message', {'user': '系统', 'msg': msg, 'type': 'info'}, room=room_id)
        finish_round(room_id, match, reason="ATK_TIMEOUT")
        return True
    return False

@socketio.on('s4_submit_target')
def on_s4_submit_target(data):
    rid, uid = data['roomId'], data['userId']
    match = get_match(rid, uid)
    if not match: return
    gd = match["game_data"]
    s4 = gd.get("s4")
    if not s4: return
    
    if s4_check_timeout(match, rid): return

    target = int(data['targetNum'])
    if not (1 <= target <= 22): return emit('error', {'msg': '数字必须在1-22之间'})

    if s4["stage"] == 0 and match["attacker"] == uid:
        s4["target_x"] = target
        s4["stage"] = 1
        socketio.emit('chat_message', {'user': '系统', 'msg': f'攻方设定目标为寻找 {target} 张10元币。开始揭示7个盒子。', 'type': 'info'}, room=rid)
        broadcast_game_state(rid)
        
    elif s4["stage"] == 2 and match["defender"] == uid:
        s4["target_y"] = target
        s4["stage"] = 3
        socketio.emit('chat_message', {'user': '系统', 'msg': f'守方设定目标为寻找 {target} 张10元币。攻方继续揭示7个盒子。', 'type': 'info'}, room=rid)
        broadcast_game_state(rid)

@socketio.on('s4_reveal')
def on_s4_reveal(data):
    rid, uid = data['roomId'], data['userId']
    match = get_match(rid, uid)
    if not match or match["attacker"] != uid: return
    gd = match["game_data"]
    s4 = gd.get("s4")
    if not s4: return

    if s4_check_timeout(match, rid): return
    
    box_idx = int(data['boxId'])
    boxes = gd["boxes"]
    
    # 阶段 1
    if s4["stage"] == 1:
        if box_idx in s4["revealed_phase1"] or box_idx in s4["revealed_phase2"]: return
        s4["revealed_phase1"].append(box_idx)
        
        real_box = boxes[box_idx]
        gd["public_boxes"][box_idx].update({"revealed": True, "real_c10": real_box['c10'], "real_c100": real_box['c100']})
        
        if len(s4["revealed_phase1"]) >= 7:
            found = any(boxes[i]['c10'] == s4["target_x"] for i in s4["revealed_phase1"])
            msg = f"✅ 第一阶段目标 [{s4['target_x']}] 寻找成功！" if found else f"❌ 第一阶段目标 [{s4['target_x']}] 寻找失败。"
            if found: s4["wins"] += 1
            
            socketio.emit('chat_message', {'user': '系统', 'msg': msg, 'type': 'info'}, room=rid)
            
            # 【重置显示】
            for pb in gd["public_boxes"]:
                pb["revealed"] = False
                pb["real_c10"] = 0
                pb["real_c100"] = 0
            
            s4["stage"] = 2 
            
    # 阶段 3
    elif s4["stage"] == 3:
        if box_idx in s4["revealed_phase2"]: return
        s4["revealed_phase2"].append(box_idx)
        
        real_box = boxes[box_idx]
        gd["public_boxes"][box_idx].update({"revealed": True, "real_c10": real_box['c10'], "real_c100": real_box['c100']})
        
        if len(s4["revealed_phase2"]) >= 7:
            found = any(boxes[i]['c10'] == s4["target_y"] for i in s4["revealed_phase2"])
            msg = f"✅ 第二阶段目标 [{s4['target_y']}] 寻找成功！" if found else f"❌ 第二阶段目标 [{s4['target_y']}] 寻找失败。"
            if found: s4["wins"] += 1
            
            socketio.emit('chat_message', {'user': '系统', 'msg': msg, 'type': 'info'}, room=rid)
            
            # 【重置显示】
            for pb in gd["public_boxes"]:
                pb["revealed"] = False
                pb["real_c10"] = 0
                pb["real_c100"] = 0
                
            s4["stage"] = 4
            wins = s4["wins"]
            pick_count = 22 if wins == 2 else (7 if wins == 1 else 5)
            socketio.emit('chat_message', {'user': '系统', 'msg': f"最终结果：成功 {wins} 次。攻方可选择 {pick_count} 个盒子拿走。", 'type': 'info'}, room=rid)

    broadcast_game_state(rid)

@socketio.on('s4_execute_pick')
def on_s4_execute_pick(data):
    rid, uid = data['roomId'], data['userId']
    match = get_match(rid, uid)
    if not match or match["attacker"] != uid: return
    gd = match["game_data"]
    s4 = gd.get("s4")
    if not s4 or s4["stage"] != 4: return
    
    if s4_check_timeout(match, rid): return

    indices = data['pickIndices']
    wins = s4["wins"]
    allowed_count = 22 if wins == 2 else (7 if wins == 1 else 5)
    
    if allowed_count != 22 and len(indices) != allowed_count:
        return emit('error', {'msg': f'必须精确选择 {allowed_count} 个盒子'})

    profit = 0
    boxes = gd["boxes"]
    
    for i in indices:
        if i < 0 or i >= 22: continue
        raw = boxes[i]
        if raw.get('taken', False): continue
        
        val_c10 = raw['c10']
        val_c100 = raw['c100']
        current_val = val_c10 * 10 + val_c100 * 100
        
        profit += current_val
        raw['c10'] = 0; raw['c100'] = 0; raw['taken'] = True
        
        gd["public_boxes"][i].update({
            "revealed": True, "taken": True, 
            "real_c10": val_c10, "real_c100": val_c100
        })

    rooms[rid]["scores"][match["attacker"]] += profit
    msg = f"方案D结算完毕，攻方掠夺了 {len(indices)} 个盒子，共计获得 {profit} 代币"
    socketio.emit('chat_message', {'user': '系统', 'msg': msg, 'type': 'info'}, room=rid)
    finish_round(rid, match, reason="NORMAL", penalty_data={'def_delta': 0, 'atk_delta': profit})

# --- 其他策略 ---

@socketio.on('s2_reveal_first')
def on_s2_reveal_first(data):
    rid = data['roomId']
    match = get_match(rid, data['userId'])
    if not match: return
    idx = data['boxId']
    gd = match["game_data"]
    box = gd["boxes"][idx]
    gd["public_boxes"][idx].update({"revealed":True, "real_c10":box['c10'], "real_c100":box['c100'], "taken":True})
    broadcast_game_state(rid)

@socketio.on('execute_attack')
def on_attack(data):
    rid, uid = data['roomId'], data['userId']
    room = rooms[rid]
    match = get_match(rid, uid)
    if not match or match["attacker"] != uid: return
    
    gd = match["game_data"]
    boxes = gd["boxes"]
    atk = match["attacker"]
    strat = gd["strategy"]
    profit = 0; msg = ""; done = False
    
    if strat == 1:
        idx = data['boxId']
        raw = boxes[idx]
        val = raw['c10']*10 + raw['c100']*100
        profit = val
        gd["public_boxes"][idx].update({"revealed":True, "taken":True, "real_c10":raw['c10'], "real_c100":raw['c100']})
        raw.update({'c10':0, 'c100':0, 'taken':True})
        gd["attempts"] -= 1
        msg = f"盲选 #{idx} 获得 {val}"
        if gd["attempts"] <= 0: done = True

    elif strat == 2:
        iA, iB = data['boxA'], data['boxB']
        guess = data['guess']
        val_A = boxes[iA]['c10']*10 + boxes[iA]['c100']*100
        val_B = boxes[iB]['c10']*10 + boxes[iB]['c100']*100
        
        win = False
        if guess == 'more':
            if val_B >= val_A: win = True
        elif guess == 'less':
            if val_B < val_A: win = True
            
        raw_B = boxes[iB]
        gd["public_boxes"][iB].update({"revealed":True, "taken":True, "real_c10":raw_B['c10'], "real_c100":raw_B['c100']})
        
        if win:
            profit = val_A + val_B
            msg = f"推演成功！获得 {profit}"
            boxes[iA].update({'c10':0, 'c100':0, 'taken':True})
            boxes[iB].update({'c10':0, 'c100':0, 'taken':True})
        else:
            msg = "推演失败，未获得资金"
            boxes[iA].update({'taken':True})
            boxes[iB].update({'taken':True})
        gd["attempts"] -= 1
        if gd["attempts"] <= 0: done = True

    elif strat == 3:
        g = int(data['guessIdx'])
        gd["guesses"] = gd.get("guesses", 0) + 1
        spec_idx = next((i for i, b in enumerate(boxes) if b['c100'] > 0), 0)
        is_lucky_turn = gd["guesses"] in [1,2,3,7,8]
        hint = "大了" if g > spec_idx else "小了"
        if g == spec_idx:
            if is_lucky_turn:
                all_profit = sum([b['c10']*10 + b['c100']*100 for b in boxes])
                profit = all_profit
                msg = f"追踪成功！掠夺全场 {profit}"
                for i in range(22):
                    r = boxes[i]
                    gd["public_boxes"][i].update({"revealed":True, "taken":True, "real_c10":r['c10'], "real_c100":r['c100']})
                    r.update({'c10':0, 'c100':0, 'taken':True})
            else:
                msg = f"定位成功但时机不对(第{gd['guesses']}次)，行动失败"
                r = boxes[spec_idx]
                gd["public_boxes"][spec_idx].update({"revealed":True, "taken":True, "real_c10":r['c10'], "real_c100":r['c100']})
            done = True
        else:
            msg = f"追踪偏离 ({hint})"
            socketio.emit('strat3_hint', {'hint': hint, 'count': gd["guesses"]}, room=rid)
    
    room["scores"][atk] += profit
    gd["last_msg"] = msg
    socketio.emit('chat_message', {'user': '系统', 'msg': f"{atk} {msg}", 'type': 'info'}, room=rid)
    if done: finish_round(rid, match, reason="NORMAL", penalty_data={'def_delta': 0, 'atk_delta': profit})
    else: broadcast_game_state(rid)

def get_match(rid, uid):
    room = rooms.get(rid)
    if not room: return None
    for m in room["matches"].values():
        if uid in [m["p1"], m["p2"]]: return m
    return None

# --- 结算与重置 ---

def finish_round(rid, match, reason="NORMAL", penalty_data=None):
    room = rooms.get(rid)
    if not room: return
    
    defender = match["defender"]
    attacker = match["attacker"]
    gd = match["game_data"]
    
    # 1. 剩余资金回收
    refund_total = 0
    for b in gd["boxes"]:
        if not b.get('taken', False):
            val = b['c10'] * 10 + b['c100'] * 100
            refund_total += val
    room["scores"][defender] += refund_total
    
    # 2. 构造历史记录
    if reason == "DEF_TIMEOUT":
        result_text = "防守部署超时"
        pnl_def = penalty_data['def_delta']
        pnl_atk = penalty_data['atk_delta']
    elif reason == "ATK_TIMEOUT":
        result_text = "进攻思考超时"
        pnl_def = refund_total # 守方回收
        pnl_atk = 0
    else:
        result_text = "正常结算"
        pnl_def = refund_total 
        pnl_atk = penalty_data['atk_delta'] if penalty_data else 0

    history_entry = {
        "round": match["round"],
        "defender": defender,
        "attacker": attacker,
        "rule": gd.get("rule", 0),
        "strat": gd.get("strategy", 0),
        "result": result_text,
        "pnl_def": pnl_def,
        "pnl_atk": pnl_atk
    }
    room["history"].append(history_entry)

    socketio.emit('round_summary', {
        "match_id": match["id"], "round": match["round"], 
        "scores": room["scores"], "refund": refund_total, "reason": reason
    }, room=rid)
    
    socketio.sleep(4)
    
    if match["round"] >= 6:
        handle_game_over(rid)
    else:
        match["defender"], match["attacker"] = match["attacker"], match["defender"]
        match["round"] += 1
        new_deadline = time.time() + 300
        match["game_data"] = { "step": "SETUP", "boxes": [], "rule": 0, "strategy": 0, "deadline": new_deadline }
        socketio.start_background_task(round_timer_task, rid, match["id"], match["round"])
        broadcast_game_state(rid)

def handle_game_over(rid):
    room = rooms.get(rid)
    if not room: return
    scores = room["scores"]
    winner = max(scores, key=scores.get)
    
    socketio.emit('show_game_summary', {
        "history": room["history"],
        "scores": scores,
        "winner": winner
    }, room=rid)
    
    timer_id = str(uuid.uuid4())
    reset_timers[rid] = timer_id
    socketio.start_background_task(auto_reset_task, rid, timer_id)

def auto_reset_task(rid, timer_id):
    socketio.sleep(180) 
    if rid in reset_timers and reset_timers[rid] == timer_id:
        reset_room_logic(rid)

@socketio.on('confirm_summary')
def on_confirm_summary(data):
    rid, uid = data.get('roomId'), data.get('userId')
    room = rooms.get(rid)
    if not room: return
    
    # 确认逻辑
    if uid not in room['summary_confirms']:
        room['summary_confirms'].append(uid)
        
    count = len(room['summary_confirms'])
    total = len(room['players'])
    socketio.emit('update_confirm_count', count, room=rid)
    if count >= total:
        reset_room_logic(rid)

@socketio.on('get_room_sync')
def on_get_sync(data):
    rid = data.get('roomId')
    if rid in rooms:
        emit('room_sync', rooms[rid])

def reset_room_logic(rid):
    room = rooms.get(rid)
    if not room: return
    if rid in reset_timers: del reset_timers[rid]
    
    room['state'] = "LOBBY"
    room['matches'] = {}
    room['history'] = []
    room['summary_confirms'] = []
    room['ready'] = {uid: False for uid in room['players']}
    room['scores'] = {uid: 10000 for uid in room['players']}
    room['chat_history'].append({'user':'系统', 'msg':'游戏已重置，请重新准备', 'type':'info'})
    
    # 【修复】使用 socketio.emit，因为此函数可能在后台任务中调用，没有上下文
    socketio.emit('reset_to_lobby', room=rid)
    socketio.emit('lobby_update', get_lobby_data())
    socketio.emit('room_sync', room, room=rid)

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5005, debug=True)