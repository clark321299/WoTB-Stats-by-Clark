/* CL-Stats session warnings — synced with Cl-Stats-Web */
function _sessionBattleTotal(sess) {
    return (sess.diffs || []).reduce((s, d) => s + (d.battles || 0), 0);
}

function _ensureSessionPrimaryFlags(sessions) {
    const byAcc = {};
    sessions.forEach((s, i) => {
        const k = String(s.accountId || "unknown");
        if (!byAcc[k]) byAcc[k] = [];
        byAcc[k].push(i);
    });
    let changed = false;
    for (const indices of Object.values(byAcc)) {
        const primaries = indices.filter(i => sessions[i].isPrimary);
        if (primaries.length > 1) {
            for (let j = 1; j < primaries.length; j++) {
                sessions[primaries[j]].isPrimary = false;
                changed = true;
            }
            continue;
        }
        if (primaries.length === 1) continue;
        const allNamed = indices.filter(i => /^all$/i.test(String(sessions[i].name || "").trim()));
        if (!allNamed.length) continue;
        let pick = allNamed[0];
        if (allNamed.length > 1) {
            pick = allNamed.reduce((best, i) => _sessionBattleTotal(sessions[i]) > _sessionBattleTotal(sessions[best]) ? i : best, allNamed[0]);
        }
        sessions[pick].isPrimary = true;
        changed = true;
    }
    return changed;
}

function toggleSessionPrimary(idx) {
    const sessions = getSessions();
    const s = sessions[idx];
    if (!s) return;
    const accId = String(s.accountId || "unknown");
    if (s.isPrimary) {
        s.isPrimary = false;
    } else {
        sessions.forEach((x, i) => {
            if (String(x.accountId || "unknown") === accId) x.isPrimary = (i === idx);
        });
    }
    saveSessions(sessions);
    renderSessions();
}

// ==================== SESSION BEST/WORST TANK ====================
function fmtAdvice(template, vars) {
    let s = template;
    for (const [k, v] of Object.entries(vars)) s = s.split(`{${k}}`).join(v);
    return s;
}

function _adviceWindowSize(totalSessionBattles) {
    if (totalSessionBattles <= 80) return totalSessionBattles;
    if (totalSessionBattles <= 200) return 40;
    if (totalSessionBattles <= 600) return 30;
    return 25;
}

function _diffTankDelta(cur, old) {
    old = old || {};
    const bd = (cur.battles || 0) - (old.battles || 0);
    if (bd <= 0) return null;
    return {
        tank_id: cur.tank_id,
        tank_name: cur.tank_name, tank_tier: cur.tank_tier, tank_type: cur.tank_type,
        tank_image: cur.tank_image, is_premium: cur.is_premium,
        battles: bd,
        wins: (cur.wins || 0) - (old.wins || 0),
        damage_dealt: (cur.damage_dealt || 0) - (old.damage_dealt || 0),
        frags: (cur.frags || 0) - (old.frags || 0),
        survived: (cur.survived || 0) - (old.survived || 0),
        spotted: (cur.spotted || 0) - (old.spotted || 0),
        hits: (cur.hits || 0) - (old.hits || 0),
        shots: (cur.shots || 0) - (old.shots || 0),
        damage_received: (cur.damage_received || 0) - (old.damage_received || 0)
    };
}

function _diffsFromSnap(curDiffs, baseTankDiffs) {
    const oldMap = {};
    (baseTankDiffs || []).forEach(d => { oldMap[d.tank_id] = d; });
    const out = [];
    (curDiffs || []).forEach(d => {
        const delta = _diffTankDelta(d, oldMap[d.tank_id]);
        if (delta) out.push(delta);
    });
    return out;
}

function getRecentSessionDiffs(sess) {
    const cur = sess.diffs || [];
    const totalNow = cur.reduce((s, d) => s + d.battles, 0);
    if (totalNow === 0) return { diffs: [], window: 0, totalNow: 0, needRefresh: false };

    const hist = (sess.refreshHistory || []).filter(h => h.tankDiffs && h.tankDiffs.length);

    // 1) Since last refresh — best for Day/Week/online tips
    if (hist.length >= 1) {
        const lastSnap = hist[hist.length - 1];
        if (lastSnap.battles < totalNow) {
            const sinceLast = _diffsFromSnap(cur, lastSnap.tankDiffs);
            const sinceTotal = sinceLast.reduce((s, d) => s + d.battles, 0);
            if (sinceTotal >= 6) {
                return { diffs: sinceLast, window: sinceTotal, totalNow, needRefresh: false, sinceRefresh: true };
            }
        }
    }

    const window = _adviceWindowSize(totalNow);
    if (totalNow <= window) {
        return { diffs: cur, window: totalNow, totalNow, needRefresh: false };
    }

    if (!hist.length) {
        return { diffs: [], window, totalNow, needRefresh: true };
    }

    const target = totalNow - window;
    let baseSnap = null;
    for (let i = hist.length - 1; i >= 0; i--) {
        if (hist[i].battles <= target) { baseSnap = hist[i]; break; }
    }
    if (!baseSnap && hist.length >= 2) baseSnap = hist[0];
    if (!baseSnap?.tankDiffs) {
        return { diffs: [], window, totalNow, needRefresh: !hist.length || hist.length < 2 };
    }

    const recent = _diffsFromSnap(cur, baseSnap.tankDiffs);
    const recentTotal = recent.reduce((s, d) => s + d.battles, 0);
    if (recentTotal < 8) {
        return { diffs: [], window, totalNow, needRefresh: recentTotal === 0 };
    }
    return { diffs: recent, window: recentTotal, totalNow, needRefresh: false };
}

function _filterAdviceTanks(recentDiffs, recentTotal, playMode) {
    if (playMode?.rotating) return [];
    let minTankBattles = recentTotal > 50 ? 4 : 3;
    if (playMode?.softRotate) minTankBattles = 5;
    const minShare = recentTotal > 30 ? 0.12 : 0.08;
    return (recentDiffs || []).filter(d => {
        if (d.battles < minTankBattles) return false;
        if (d.battles / recentTotal < minShare && d.battles < 5) return false;
        return true;
    });
}

const ADVICE_COOLDOWN_OTHER = 10;
const ADVICE_RETRY_TANK = 5;
const ADVICE_RETRY_CLASS = 5;

function _advicePlayMode(recentDiffs, recentTotal) {
    const active = (recentDiffs || []).filter(d => (d.battles || 0) >= 1);
    const tankCount = active.length;
    const avgPerTank = tankCount > 0 ? recentTotal / tankCount : recentTotal;
    const rotating = tankCount >= 3 && avgPerTank <= 4.5;
    const softRotate = !rotating && tankCount >= 2 && avgPerTank <= 5;
    return { rotating, softRotate, tankCount, avgPerTank };
}

function _classBattlesInSession(sess, classType) {
    return (sess.diffs || [])
        .filter(d => d.tank_type === classType)
        .reduce((s, d) => s + (d.battles || 0), 0);
}

function _buildClassAdviceData(recentDiffs) {
    const byClass = {};
    for (const d of recentDiffs || []) {
        if ((d.battles || 0) < 1) continue;
        const cls = d.tank_type || "?";
        if (!byClass[cls]) byClass[cls] = { tanks: [], battles: 0, wins: 0, damage: 0, tierW: 0, tierBattles: 0 };
        byClass[cls].tanks.push(d);
        byClass[cls].battles += d.battles;
        byClass[cls].wins += d.wins;
        byClass[cls].damage += d.damage_dealt;
        const tn = _tierDmgNorm(d.tank_tier);
        byClass[cls].tierW += tn * d.battles;
        byClass[cls].tierBattles += d.battles;
    }
    return Object.entries(byClass).map(([cls, v]) => {
        const wr = v.battles > 0 ? v.wins / v.battles * 100 : 0;
        const avgDmg = v.battles > 0 ? v.damage / v.battles : 0;
        const tierNorm = v.tierBattles > 0 ? v.tierW / v.tierBattles : 2000;
        const dmgRatio = tierNorm > 0 ? avgDmg / tierNorm : 1;
        const activeTanks = v.tanks.filter(t => t.battles >= 2);
        const tankNames = activeTanks.map(t => t.tank_name).slice(0, 3).join(", ") || v.tanks[0]?.tank_name || "?";
        return { cls, battles: v.battles, wr, avgDmg, dmgRatio, activeTanks: activeTanks.length, tankNames, tanks: v.tanks };
    }).sort((a, b) => a.wr - b.wr);
}

function _evalClassSameBad(data, playMode) {
    if (!data || data.battles < 4) return null;
    const minBattles = playMode.rotating ? 5 : 6;
    const minTanks = playMode.rotating ? 2 : 2;
    const wrCap = playMode.rotating ? 44 : 40;
    const dmgCap = playMode.rotating ? 0.85 : 0.82;
    if (data.activeTanks >= minTanks && data.battles >= minBattles && data.wr < wrCap && data.dmgRatio < dmgCap) {
        return {
            severity: data.wr < 34 ? "warn" : "tip",
            key: "sessAdvClassSameBad",
            score: 62 + (wrCap - data.wr),
            vars: {
                class: _adviceClassLabel(data.cls),
                tanks: data.tankNames,
                battles: String(data.battles),
                wr: data.wr.toFixed(0),
                left: String(ADVICE_COOLDOWN_OTHER)
            }
        };
    }
    if (data.battles >= 8 && data.wr < 36 && data.dmgRatio < 0.8) {
        return {
            severity: "tip",
            key: "sessAdvClassSameBad",
            score: 58 + (36 - data.wr),
            vars: {
                class: _adviceClassLabel(data.cls),
                tanks: data.tankNames,
                battles: String(data.battles),
                wr: data.wr.toFixed(0),
                left: String(ADVICE_COOLDOWN_OTHER)
            }
        };
    }
    return null;
}

function _evalClassSwitch(worst, best) {
    if (!worst || !best || worst.cls === best.cls) return null;
    if (worst.battles < 8 || best.battles < 6) return null;
    if (worst.wr >= 36 || best.wr < worst.wr + 12) return null;
    return {
        severity: "tip",
        key: "sessAdvClassSwitch",
        score: 55 + (36 - worst.wr),
        vars: {
            class: _adviceClassLabel(worst.cls),
            battles: String(worst.battles),
            wr: worst.wr.toFixed(0),
            altClass: _adviceClassLabel(best.cls),
            altWr: best.wr.toFixed(0),
            left: String(ADVICE_COOLDOWN_OTHER)
        }
    };
}

function _classBlockProgress(block, sess) {
    const totalNow = _sessionTotalBattles(sess);
    const classNow = _classBattlesInSession(sess, block.classType);
    const newTotal = totalNow - (block.blockAtTotal || 0);
    const newOnClass = classNow - (block.blockAtClassBattles || 0);
    const otherSince = Math.max(0, newTotal - newOnClass);
    return { otherSince, newOnClass, remaining: Math.max(0, ADVICE_COOLDOWN_OTHER - otherSince) };
}

function _tierDmgNorm(tier) {
    const t = parseInt(tier, 10) || 10;
    const scale = { 9: 0.88, 8: 0.76, 7: 0.65, 6: 0.58, 5: 0.52, 4: 0.46, 3: 0.40, 2: 0.35, 1: 0.30 };
    return Math.round(2000 * (scale[t] || 1));
}

function _adviceClassLabel(type) {
    return t(TYPE_KEY[type] || type || "?");
}

function _sessionTotalBattles(sess) {
    return (sess.diffs || []).reduce((s, d) => s + (d.battles || 0), 0);
}

function _tankDiffById(sess, tankId) {
    return (sess.diffs || []).find(d => String(d.tank_id) === String(tankId));
}

function _advicePeerStats(recentDiffs, recentTotal, playMode) {
    const eligible = _filterAdviceTanks(recentDiffs || [], recentTotal || 0, playMode);
    let peerWr = 0, peerW = 0;
    for (const d of eligible) {
        peerWr += (d.wins / d.battles * 100) * d.battles;
        peerW += d.battles;
    }
    if (peerW === 0) {
        for (const d of recentDiffs || []) {
            if (d.battles < 2) continue;
            peerWr += (d.wins / d.battles * 100) * d.battles;
            peerW += d.battles;
        }
    }
    return { eligible, sessAvgWr: peerW > 0 ? peerWr / peerW : 50 };
}

function _buildTankAdviceVars(d, sessAvgWr) {
    const sessWR = d.wins / d.battles * 100;
    const sessDmg = d.damage_dealt / d.battles;
    const tierNorm = _tierDmgNorm(d.tank_tier);
    const dmgRatio = tierNorm > 0 ? sessDmg / tierNorm : 1;
    return {
        tank: d.tank_name || "?",
        battles: String(d.battles),
        wr: sessWR.toFixed(0),
        tier: TIER_ROMAN[d.tank_tier] || String(d.tank_tier || "?"),
        avgDmg: Math.round(sessDmg).toLocaleString(),
        dmgPct: String(Math.round(dmgRatio * 100)),
        surv: ((d.survived || 0) / d.battles * 100).toFixed(0),
        normWr: sessAvgWr.toFixed(0),
        left: "0"
    };
}

function _evalTankBad(d, sessAvgWr) {
    const sessWR = d.wins / d.battles * 100;
    const sessDmg = d.damage_dealt / d.battles;
    const tierNorm = _tierDmgNorm(d.tank_tier);
    const dmgRatio = tierNorm > 0 ? sessDmg / tierNorm : 1;
    const sessSurv = (d.survived || 0) / d.battles * 100;
    const wrVsPeer = sessWR - sessAvgWr;
    const conf = Math.min(1, Math.max(0, (d.battles - 2) / 6));
    const vars = _buildTankAdviceVars(d, sessAvgWr);

    if (sessWR < 38 && dmgRatio < 0.78 && d.battles >= 4 && conf >= 0.25) {
        return { severity: sessWR < 32 ? "warn" : "tip", key: "sessAdvSwitch", vars, score: 50 + (38 - sessWR) };
    }
    if (sessWR < 42 && wrVsPeer < -10 && dmgRatio < 0.85 && d.battles >= 4) {
        return { severity: "tip", key: "sessAdvSwitchSoft", vars, score: 35 };
    }
    if (sessSurv <= 35 && dmgRatio < 0.8 && sessWR < 48) {
        return { severity: "tip", key: "sessAdvSafe", vars, score: 28 };
    }
    if (sessWR < 40 && dmgRatio >= 0.85 && sessSurv <= 40) {
        return { severity: "tip", key: "sessAdvRisky", vars, score: 22 };
    }
    if (sessWR < 45 && wrVsPeer < -8 && d.battles <= 5) {
        return { severity: "hint", key: "sessAdvSlight", vars, score: 12 };
    }
    return null;
}

function _adviceBlockProgress(block, sess) {
    const totalNow = _sessionTotalBattles(sess);
    const tankNow = _tankDiffById(sess, block.tank_id);
    const tankBattlesNow = tankNow?.battles || 0;
    const newTotal = totalNow - (block.blockAtTotal || 0);
    const newOnTank = tankBattlesNow - (block.blockAtTank || 0);
    const otherSince = Math.max(0, newTotal - newOnTank);
    return { otherSince, newOnTank, remaining: Math.max(0, ADVICE_COOLDOWN_OTHER - otherSince) };
}

function _aggregateAdviceByClass(recentDiffs) {
    return _buildClassAdviceData(recentDiffs).slice().sort((a, b) => b.wr - a.wr);
}

function _updateClassAdviceBlocks(sess, recent, recentTotal, playMode) {
    if (!sess.adviceClassBlocks) sess.adviceClassBlocks = {};
    const blocks = sess.adviceClassBlocks;
    let changed = false;
    const totalNow = _sessionTotalBattles(sess);
    const classData = _buildClassAdviceData(recent);
    const classMap = {};
    classData.forEach(c => { classMap[c.cls] = c; });

    for (const cls of Object.keys({ ...blocks })) {
        const block = blocks[cls];
        const prog = _classBlockProgress(block, sess);
        const data = classMap[cls];

        if (prog.otherSince >= ADVICE_COOLDOWN_OTHER) {
            if (prog.newOnClass >= ADVICE_RETRY_CLASS) {
                const bad = data ? (_evalClassSameBad(data, playMode) || null) : null;
                if (bad) {
                    block.blockAtTotal = totalNow - (data?.battles || 0);
                    block.blockAtClassBattles = _classBattlesInSession(sess, cls) - (data?.battles || 0);
                    block.key = bad.key;
                    block.severity = bad.severity;
                    block.score = bad.score;
                    block.vars = { ...bad.vars, left: String(ADVICE_COOLDOWN_OTHER) };
                    changed = true;
                } else {
                    delete blocks[cls];
                    changed = true;
                }
            } else {
                delete blocks[cls];
                changed = true;
            }
            continue;
        }

        if (data) {
            const bad = _evalClassSameBad(data, playMode);
            if (bad) {
                block.key = bad.key;
                block.severity = bad.severity;
                block.score = bad.score;
                block.vars = { ...bad.vars, left: String(prog.remaining) };
                changed = true;
            } else {
                // Клас вирівнявся — не тримаємо заморожене «зміни клас» з старим WR
                delete blocks[cls];
                changed = true;
            }
        } else {
            block.vars = { ...(block.vars || {}), left: String(prog.remaining) };
        }
    }

    const worst = classData[0];
    const best = classData[classData.length - 1];
    const switchTip = worst && best ? _evalClassSwitch(worst, best) : null;

    for (const data of classData) {
        const cls = data.cls;
        if (blocks[cls]) continue;
        let bad = _evalClassSameBad(data, playMode);
        if (!bad && switchTip && worst?.cls === cls) bad = switchTip;
        if (!bad) continue;
        blocks[cls] = {
            classType: cls,
            blockAtTotal: totalNow,
            blockAtClassBattles: _classBattlesInSession(sess, cls),
            key: bad.key,
            severity: bad.severity,
            score: bad.score,
            vars: { ...bad.vars, left: String(ADVICE_COOLDOWN_OTHER) }
        };
        changed = true;
        break;
    }

    return changed;
}

function updateSessionAdviceBlocks(sess) {
    if (sess.isPrimary) {
        let changed = false;
        if (sess.adviceBlocks && Object.keys(sess.adviceBlocks).length) {
            sess.adviceBlocks = {};
            changed = true;
        }
        if (sess.adviceClassBlocks && Object.keys(sess.adviceClassBlocks).length) {
            sess.adviceClassBlocks = {};
            changed = true;
        }
        return changed;
    }
    const { diffs: recent } = getRecentSessionDiffs(sess);
    const recentTotal = recent.reduce((s, d) => s + d.battles, 0);
    if (recentTotal < 6) return false;

    const playMode = _advicePlayMode(recent, recentTotal);
    let changed = _updateClassAdviceBlocks(sess, recent, recentTotal, playMode);

    if (!sess.adviceBlocks) sess.adviceBlocks = {};
    const blocks = sess.adviceBlocks;

    if (playMode.rotating) {
        if (Object.keys(blocks).length) {
            sess.adviceBlocks = {};
            changed = true;
        }
        return changed;
    }

    const totalNow = _sessionTotalBattles(sess);
    const recentMap = {};
    recent.forEach(d => { recentMap[d.tank_id] = d; });
    const { eligible, sessAvgWr } = _advicePeerStats(recent, recentTotal, playMode);

    for (const tid of Object.keys({ ...blocks })) {
        const block = blocks[tid];
        const prog = _adviceBlockProgress(block, sess);

        if (prog.otherSince >= ADVICE_COOLDOWN_OTHER) {
            if (prog.newOnTank >= ADVICE_RETRY_TANK) {
                const recentTank = recentMap[tid];
                const bad = recentTank ? _evalTankBad(recentTank, sessAvgWr) : null;
                if (bad) {
                    const tankFull = _tankDiffById(sess, tid);
                    block.blockAtTotal = totalNow - (recentTank.battles || 0);
                    block.blockAtTank = (tankFull?.battles || 0) - (recentTank.battles || 0);
                    block.key = bad.key;
                    block.severity = bad.severity;
                    block.score = bad.score;
                    block.vars = { ...bad.vars, left: String(ADVICE_COOLDOWN_OTHER) };
                    block.d = recentTank;
                    changed = true;
                } else {
                    delete blocks[tid];
                    changed = true;
                }
            } else {
                delete blocks[tid];
                changed = true;
            }
            continue;
        }

        // Повна форма танка в сесії: якщо WR/шкода вже ок — знімаємо hold
        // (раніше лишалось «9 б. / 33%» навіть після 65% за 29 б. на тому ж танкові).
        const tankFull = _tankDiffById(sess, tid);
        if (tankFull && tankFull.battles >= 3) {
            const badFull = _evalTankBad(tankFull, sessAvgWr);
            if (!badFull) {
                delete blocks[tid];
                changed = true;
                continue;
            }
        }

        const recentTank = recentMap[tid];
        if (recentTank) {
            const bad = _evalTankBad(recentTank, sessAvgWr);
            if (bad) {
                block.key = bad.key;
                block.severity = bad.severity;
                block.score = bad.score;
                // Показуємо актуальні цифри (краще повна сесія, якщо більше каток)
                const show = (tankFull && tankFull.battles > recentTank.battles)
                    ? _buildTankAdviceVars(tankFull, sessAvgWr)
                    : bad.vars;
                block.vars = { ...show, left: String(prog.remaining) };
                block.d = tankFull && tankFull.battles > recentTank.battles ? tankFull : recentTank;
                changed = true;
            } else {
                delete blocks[tid];
                changed = true;
            }
        } else if (tankFull) {
            const bad = _evalTankBad(tankFull, sessAvgWr);
            if (bad) {
                block.key = bad.key;
                block.severity = bad.severity;
                block.score = bad.score;
                block.vars = { ...bad.vars, left: String(prog.remaining) };
                block.d = tankFull;
                changed = true;
            } else {
                delete blocks[tid];
                changed = true;
            }
        } else {
            block.vars = { ...(block.vars || {}), left: String(prog.remaining) };
        }
    }

    for (const d of eligible) {
        const tid = d.tank_id;
        if (blocks[tid]) continue;
        const badClass = sess.adviceClassBlocks?.[d.tank_type];
        if (badClass) continue;
        const bad = _evalTankBad(d, sessAvgWr);
        if (!bad) continue;
        const tankFull = _tankDiffById(sess, tid);
        blocks[tid] = {
            tank_id: tid,
            blockAtTotal: totalNow,
            blockAtTank: tankFull?.battles || d.battles,
            key: bad.key,
            severity: bad.severity,
            score: bad.score,
            vars: { ...bad.vars, left: String(ADVICE_COOLDOWN_OTHER) },
            d
        };
        changed = true;
    }

    return changed;
}

function getSessionAdviceTips(sess) {
    const { diffs: recent, window, totalNow, needRefresh, sinceRefresh } = getRecentSessionDiffs(sess);
    const recentTotal = recent.reduce((s, d) => s + d.battles, 0);
    const playMode = _advicePlayMode(recent, recentTotal);
    const meta = { recentTotal, sinceRefresh, window, totalNow, needRefresh: !!(needRefresh && totalNow > 80), playMode };
    if (meta.needRefresh) return { tips: [], meta };

    updateSessionAdviceBlocks(sess);
    const tips = [];

    for (const block of Object.values(sess.adviceClassBlocks || {})) {
        const prog = _classBlockProgress(block, sess);
        tips.push({
            severity: block.severity || "warn",
            key: prog.remaining > 0 ? "sessAdvClassHold" : block.key,
            vars: { ...(block.vars || {}), left: String(prog.remaining) },
            score: (block.score || 60) + prog.remaining * 0.15,
            d: null
        });
    }

    if (!playMode.rotating) {
        for (const block of Object.values(sess.adviceBlocks || {})) {
            const prog = _adviceBlockProgress(block, sess);
            const d = block.d || { tank_id: block.tank_id, tank_name: block.vars?.tank, tank_image: null };
            tips.push({
                severity: block.severity || "warn",
                key: prog.remaining > 0 ? "sessAdvHold" : block.key,
                vars: { ...(block.vars || {}), left: String(prog.remaining) },
                score: (block.score || 40) + prog.remaining * 0.1,
                d
            });
        }
    }

    tips.sort((a, b) => b.score - a.score);
    const seen = new Set();
    const out = [];
    for (const tip of tips) {
        const id = tip.d?.tank_id || tip.d?.tank_name || ("cls:" + (tip.vars?.class || tip.key));
        if (seen.has(id)) continue;
        seen.add(id);
        out.push(tip);
        if (out.length >= 3) break;
    }
    return { tips: out, meta };
}

/** Desktop: hide WG «switch tank» tips (overlap with Forecast). Keep module for web later. */
const SESSION_TANK_ADVICE_ENABLED = false;

function renderSessionAdvice(sess, sessIdx) {
    if (!SESSION_TANK_ADVICE_ENABLED) return "";
    if (sess.isPrimary) return "";

    const before = JSON.stringify({ t: sess.adviceBlocks, c: sess.adviceClassBlocks });
    const { tips, meta } = getSessionAdviceTips(sess);
    if (sessIdx != null && JSON.stringify({ t: sess.adviceBlocks, c: sess.adviceClassBlocks }) !== before) {
        const sessions = getSessions();
        if (sessions[sessIdx]) {
            sessions[sessIdx].adviceBlocks = sess.adviceBlocks;
            sessions[sessIdx].adviceClassBlocks = sess.adviceClassBlocks;
            saveSessions(sessions);
        }
    }

    if (meta.needRefresh) {
        return `<div class="sess-advice">
            <div style="font-size:.78rem;color:var(--t2);font-weight:600;margin-bottom:6px;text-transform:uppercase;letter-spacing:.04em">${t("sessAdviceTitle")}</div>
            <div class="sess-advice-item sess-advice-hint"><span>${t("sessAdviceNeedRefresh")}</span></div>
        </div>`;
    }
    if (!tips.length) return "";

    const cls = { warn: "sess-advice-warn", tip: "sess-advice-tip", good: "sess-advice-good", hint: "sess-advice-hint" };
    const recentLabel = meta.recentTotal > 0 && (meta.sinceRefresh || meta.totalNow > meta.window)
        ? `<div style="font-size:.72rem;color:var(--acc);margin-bottom:6px;opacity:.9">${fmtAdvice(t(meta.sinceRefresh ? "sessAdviceRecent" : "sessAdviceRecentWin"), { n: String(meta.recentTotal) })}</div>`
        : "";
    return `<div class="sess-advice">
        <div style="font-size:.78rem;color:var(--t2);font-weight:600;margin-bottom:6px;text-transform:uppercase;letter-spacing:.04em">${t("sessAdviceTitle")}</div>
        ${recentLabel}
        ${tips.map(tip => {
            const img = tip.d?.tank_image ? `<img src="${tip.d.tank_image}" alt="" style="width:28px;height:auto;border-radius:2px;flex-shrink:0;margin-top:1px">` : "";
            return `<div class="sess-advice-item ${cls[tip.severity] || ""}">${img}<span>${fmtAdvice(t(tip.key), tip.vars)}</span></div>`;
        }).join("")}
        <div style="font-size:.72rem;color:var(--t2);margin-top:2px;opacity:.85">${t("sessAdviceNote")}</div>
    </div>`;
}

if (typeof renderSessions === "function") {
    try { renderSessions(); } catch (e) { console.warn("session_advice init:", e); }
}
