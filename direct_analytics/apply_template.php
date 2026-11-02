<?php
/*
 * Применение изменений в Яндекс Директе по кнопке «Применить» на странице отчёта.
 * Файл создаётся скриптом direct_report.py — править его вручную не нужно.
 * Применяются ТОЛЬКО предложения, которые подготовил бот (по их номеру), ничего другого этот файл делать не умеет.
 */
date_default_timezone_set('Asia/Barnaul');
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

$SETTINGS = '__SETTINGS_PATH__';

function out($a, $code = 200) { http_response_code($code); echo json_encode($a, JSON_UNESCAPED_UNICODE); exit; }
function rj($p, $d) { if (!is_file($p)) return $d; $x = json_decode(file_get_contents($p), true); return $x === null ? $d : $x; }

$S = rj($SETTINGS, null);
if (!$S) out(['ok' => false, 'error' => 'Не найдены настройки apply_settings.json — запустите direct_report.py'], 500);

$in = json_decode(file_get_contents('php://input'), true);
if (!$in) $in = $_GET;
if (!isset($in['key']) || !hash_equals($S['key'], (string)$in['key'])) out(['ok' => false, 'error' => 'Неверный ключ страницы. Обновите страницу.'], 403);
$action = isset($in['action']) ? $in['action'] : '';

/* ---------- журнал ---------- */
function log_update($fn) {
    global $S;
    $fp = fopen($S['log'], 'c+');
    flock($fp, LOCK_EX);
    $raw = stream_get_contents($fp);
    $log = $raw ? json_decode($raw, true) : [];
    if (!is_array($log)) $log = [];
    $res = $fn($log);
    ftruncate($fp, 0); rewind($fp);
    fwrite($fp, json_encode($log, JSON_UNESCAPED_UNICODE));
    fflush($fp); flock($fp, LOCK_UN); fclose($fp);
    return $res;
}

/* ---------- API Директа ---------- */
function direct($service, $method, $params) {
    global $S;
    if (!empty($S['demo'])) return demo_result($service, $method, $params);
    $h = ['Authorization: Bearer ' . $S['token'], 'Accept-Language: ru', 'Content-Type: application/json; charset=utf-8'];
    if (!empty($S['client_login'])) $h[] = 'Client-Login: ' . $S['client_login'];
    $ch = curl_init($S['api'] . '/json/v5/' . $service);
    curl_setopt_array($ch, [CURLOPT_POST => true, CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 60,
        CURLOPT_HTTPHEADER => $h, CURLOPT_POSTFIELDS => json_encode(['method' => $method, 'params' => $params], JSON_UNESCAPED_UNICODE)]);
    $body = curl_exec($ch);
    if ($body === false) { $e = curl_error($ch); curl_close($ch); throw new Exception('нет связи с API: ' . $e); }
    curl_close($ch);
    $r = json_decode($body, true);
    if (!$r) throw new Exception('непонятный ответ API');
    if (isset($r['error'])) throw new Exception($r['error']['error_string'] . ' ' . (isset($r['error']['error_detail']) ? $r['error']['error_detail'] : ''));
    // ошибки по отдельным объектам
    foreach ($r['result'] as $k => $list) {
        if (substr($k, -7) !== 'Results' || !is_array($list)) continue;
        foreach ($list as $it) if (!empty($it['Errors'])) {
            $e = $it['Errors'][0];
            throw new Exception($e['Message'] . (empty($e['Details']) ? '' : ': ' . $e['Details']));
        }
    }
    return $r['result'];
}

function demo_result($service, $method, $params) {
    if ($method === 'get' && $service === 'campaigns') return ['Campaigns' => [['Id' => $params['SelectionCriteria']['Ids'][0], 'ExcludedSites' => null, 'NegativeKeywords' => null]]];
    if ($method === 'add') return ['AddResults' => [['Id' => 99999]]];
    return ['UpdateResults' => [], 'SetResults' => []];
}

function camp_lists($cid) {
    $r = direct('campaigns', 'get', ['SelectionCriteria' => ['Ids' => [$cid]], 'FieldNames' => ['Id', 'ExcludedSites', 'NegativeKeywords']]);
    if (empty($r['Campaigns'])) throw new Exception('кампания ' . $cid . ' не найдена');
    $c = $r['Campaigns'][0];
    $ex = isset($c['ExcludedSites']['Items']) ? $c['ExcludedSites']['Items'] : [];
    $ng = isset($c['NegativeKeywords']['Items']) ? $c['NegativeKeywords']['Items'] : [];
    return [$ex, $ng];
}

function set_list($cid, $field, $items) {
    $items = array_values(array_unique($items));
    direct('campaigns', 'update', ['Campaigns' => [['Id' => $cid, $field => $items ? ['Items' => $items] : null]]]);
}

/* выполнить действие; возвращает данные для отката */
function run($type, $cid, $p, $undo = false) {
    switch ($type) {
        case 'exclude_site':
            list($ex,) = camp_lists($cid);
            if ($undo) { set_list($cid, 'ExcludedSites', array_diff($ex, [$p['site']])); return 'Площадка снова показывается'; }
            if (in_array($p['site'], $ex)) return 'Площадка уже была исключена';
            if (count($ex) >= 1000) throw new Exception('в кампании уже 1000 исключённых площадок — это максимум Директа');
            $ex[] = $p['site']; set_list($cid, 'ExcludedSites', $ex);
            return 'Площадка исключена';
        case 'add_negative':
            list(, $ng) = camp_lists($cid);
            $w = isset($p['words']) ? $p['words'] : [$p['word']];
            if ($undo) { set_list($cid, 'NegativeKeywords', array_diff($ng, $w)); return 'Минус-слова убраны'; }
            $add = array_diff($w, $ng);
            if (!$add) return 'Минус-слова уже были';
            set_list($cid, 'NegativeKeywords', array_merge($ng, $add));
            $GLOBALS['added'] = array_values($add);
            return 'Добавлено: -' . implode(' -', $add);
        case 'suspend_keyword':
            direct('keywords', $undo ? 'resume' : 'suspend', ['SelectionCriteria' => ['Ids' => [$p['keyword_id']]]]);
            return $undo ? 'Фраза снова включена' : 'Фраза остановлена';
        case 'set_bid':
            $bid = $undo ? $p['old_bid'] : $p['new_bid'];
            direct('keywordbids', 'set', ['KeywordBids' => [['KeywordId' => $p['keyword_id'], 'SearchBid' => $bid]]]);
            return 'Ставка ' . number_format($bid / 1e6, 2, ',', ' ') . ' ₽';
        case 'mobile_adjust':
            $mid = isset($p['modifier_id']) ? $p['modifier_id'] : null;
            $val = $undo ? $p['old'] : $p['new'];
            if ($mid) {
                direct('bidmodifiers', 'set', ['BidModifiers' => [['Id' => $mid, 'BidModifier' => $val]]]);
            } elseif ($undo) {
                if (!empty($p['created_id'])) direct('bidmodifiers', 'delete', ['SelectionCriteria' => ['Ids' => [$p['created_id']]]]);
            } else {
                $r = direct('bidmodifiers', 'add', ['BidModifiers' => [['CampaignId' => $cid, 'MobileAdjustment' => ['BidModifier' => $val]]]]);
                $GLOBALS['created_id'] = isset($r['AddResults'][0]['Ids'][0]) ? $r['AddResults'][0]['Ids'][0]
                    : (isset($r['AddResults'][0]['Id']) ? $r['AddResults'][0]['Id'] : null);
            }
            return 'Корректировка смартфонов ' . $val . '%';
    }
    throw new Exception('неизвестное действие');
}

/* ---------- запросы страницы ---------- */
if ($action === 'log') out(['ok' => true, 'log' => array_slice(rj($S['log'], []), -200)]);

if ($action === 'apply') {
    $P = rj($S['proposals'], null);
    if (!$P) out(['ok' => false, 'error' => 'Нет списка предложений'], 400);
    $age = (time() - strtotime($P['created'])) / 86400;
    if ($age > $S['max_age_days']) out(['ok' => false, 'error' => 'Предложения устарели (от ' . $P['created'] . '). Дождитесь нового отчёта.'], 400);
    $ids = isset($in['ids']) && is_array($in['ids']) ? array_slice($in['ids'], 0, 300) : [];
    $done = [];
    foreach (rj($S['log'], []) as $a) if ($a['status'] === 'ok' && empty($a['undone'])) $done[$a['proposal_id']] = 1;
    $results = [];
    foreach ($ids as $id) {
        if (!isset($P['items'][$id])) { $results[$id] = ['status' => 'error', 'message' => 'нет такого предложения']; continue; }
        if (isset($done[$id])) { $results[$id] = ['status' => 'ok', 'message' => 'уже применено']; continue; }
        $it = $P['items'][$id];
        $GLOBALS['created_id'] = null; $GLOBALS['added'] = null;
        try { $msg = run($it['type'], $it['campaign_id'], $it['params']); $st = 'ok'; }
        catch (Exception $e) { $msg = $e->getMessage(); $st = 'error'; }
        $params = $it['params'];
        if ($GLOBALS['created_id']) $params['created_id'] = $GLOBALS['created_id'];
        if ($GLOBALS['added']) $params['words'] = $GLOBALS['added'];   // откат уберёт только добавленное
        $entry = ['log_id' => bin2hex(random_bytes(6)), 'time' => date('d.m.Y H:i'), 'proposal_id' => $id,
                  'type' => $it['type'], 'campaign_id' => $it['campaign_id'], 'campaign' => $it['campaign'],
                  'title' => $it['title'], 'params' => $params, 'status' => $st, 'message' => $msg,
                  'ip' => isset($_SERVER['REMOTE_ADDR']) ? $_SERVER['REMOTE_ADDR'] : ''];
        log_update(function (&$log) use ($entry) { $log[] = $entry; });
        $results[$id] = ['status' => $st, 'message' => $msg];
    }
    out(['ok' => true, 'results' => $results, 'log' => array_slice(rj($S['log'], []), -200)]);
}

if ($action === 'undo') {
    $lid = isset($in['log_id']) ? $in['log_id'] : '';
    $entry = null;
    foreach (rj($S['log'], []) as $a) if ($a['log_id'] === $lid) $entry = $a;
    if (!$entry || $entry['status'] !== 'ok' || !empty($entry['undone'])) out(['ok' => false, 'error' => 'Это действие нельзя откатить'], 400);
    try { $msg = run($entry['type'], $entry['campaign_id'], $entry['params'], true); }
    catch (Exception $e) { out(['ok' => false, 'error' => 'Откат не выполнен: ' . $e->getMessage()]); }
    log_update(function (&$log) use ($lid, $msg) {
        foreach ($log as &$a) if ($a['log_id'] === $lid) { $a['undone'] = date('d.m.Y H:i'); $a['undo_message'] = $msg; }
    });
    out(['ok' => true, 'message' => $msg, 'log' => array_slice(rj($S['log'], []), -200)]);
}

out(['ok' => false, 'error' => 'Неизвестный запрос'], 400);
