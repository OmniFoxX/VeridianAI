/**
 * OracleAI — Mini-Games Module v2.1
 * FIXES: bulletproof keyboard scoping (spacebar never stolen from textarea),
 *        delta-time speed, close button stops game loop, scoreboards
 */

if (typeof Haptic === 'undefined') {
  console.warn('[OracleAI] Haptic module not loaded — using silent fallback');
  window.Haptic = {
    vibrate: function() {}, setEnabled: function() {},
    isEnabled: function() { return false; }, isSupported: function() { return false; },
    PATTERNS: { send: [15], receive: [10, 40, 10], done: [20, 30, 60],
      error: [80, 40, 80], gameScore: [30], gameDie: [60, 30, 60], toggle: [10] },
  };
}

const GameManager = (() => {
  let canvas, ctx;
  let currentGame = null;
  let currentGameName = 'asteroids';
  let animFrame = null;
  let score = 0;
  let lastTime = 0;

  function getScores(game) {
    try { return JSON.parse(localStorage.getItem(`oracle_scores_${game}`)) || []; }
    catch { return []; }
  }
  function saveScores(game, scores) {
    localStorage.setItem(`oracle_scores_${game}`, JSON.stringify(scores.slice(0, 10)));
  }

  function init() {
    canvas = document.getElementById('game-canvas');
    if (!canvas) return;
    ctx = canvas.getContext('2d');
    canvas.addEventListener('click', onCanvasClick);
    document.addEventListener('keydown', onKeyDown);
    document.addEventListener('keyup', onKeyUp);
    // Don't auto-start — wait for user to open game panel
  }

  function start(name) {
    stop();
    score = 0;
    currentGameName = name;
    updateScore(0);
    renderScoreboard(name);

    if (name === 'asteroids') currentGame = Asteroids;
    else if (name === 'snake') currentGame = Snake;
    else if (name === 'tictactoe') currentGame = TicTacToe;
    else return;

    currentGame.init(canvas, ctx);
    lastTime = performance.now();
    loop(lastTime);

    const controlsEl = document.getElementById('game-controls');
    if (controlsEl) controlsEl.textContent = currentGame.controls || '↑↓←→  Move\nSpace  Action';
  }

  function stop() {
    if (animFrame) { cancelAnimationFrame(animFrame); animFrame = null; }
    if (currentGame && currentGame.cleanup) currentGame.cleanup();
    currentGame = null;
  }

  function loop(timestamp) {
    animFrame = requestAnimationFrame(loop);
    const dt = Math.min((timestamp - lastTime) / 1000, 0.05);
    lastTime = timestamp;
    if (currentGame) {
      const s = currentGame.update(dt);
      currentGame.draw(ctx);
      if (typeof s === 'number' && s !== score) {
        score = s;
        updateScore(score);
      }
    }
  }

  function isPanelVisible() {
    const panel = document.getElementById('oracle-panel');
    return panel && panel.classList.contains('visible');
  }

  /**
   * FIX: Bulletproof keyboard scoping.
   * Space bar (and arrows) are ONLY captured when:
   *   1. The game panel is visible AND
   *   2. The active element is NOT any form input (textarea, input, select)
   * This guarantees typing in the chat field is never disrupted.
   */
  function onKeyDown(e) {
    if (!currentGame || !isPanelVisible()) return;

    // Check BOTH the event target AND the currently focused element
    const targetTag = (e.target.tagName || '').toUpperCase();
    const activeTag = document.activeElement ? document.activeElement.tagName.toUpperCase() : '';
    const formElements = ['INPUT', 'TEXTAREA', 'SELECT'];

    if (formElements.includes(targetTag) || formElements.includes(activeTag)) return;

    // Also check contentEditable
    if (e.target.isContentEditable || (document.activeElement && document.activeElement.isContentEditable)) return;

    const gameKeys = ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'];
    const isArrow = gameKeys.includes(e.key);
    const isSpace = e.key === ' ' || e.code === 'Space';

    if (isArrow || isSpace) {
      e.preventDefault();
      const mappedKey = isSpace ? 'Space' : e.key;
      if (currentGame.keydown) currentGame.keydown(mappedKey);
    }
  }

  function onKeyUp(e) {
    if (!currentGame || !isPanelVisible()) return;

    const targetTag = (e.target.tagName || '').toUpperCase();
    const activeTag = document.activeElement ? document.activeElement.tagName.toUpperCase() : '';
    const formElements = ['INPUT', 'TEXTAREA', 'SELECT'];

    if (formElements.includes(targetTag) || formElements.includes(activeTag)) return;
    if (e.target.isContentEditable || (document.activeElement && document.activeElement.isContentEditable)) return;

    const k = (e.key === ' ' || e.code === 'Space') ? 'Space' : e.key;
    if (currentGame.keyup) currentGame.keyup(k);
  }

  function onCanvasClick(e) {
    if (!currentGame || !currentGame.click) return;
    const rect = canvas.getBoundingClientRect();
    currentGame.click(e.clientX - rect.left, e.clientY - rect.top);
  }

  function updateScore(s) {
    const el = document.getElementById('game-score');
    if (el) el.textContent = `Score: ${s}`;
  }

  function checkHighScore(gameName, finalScore) {
    if (finalScore <= 0) return;
    const scores = getScores(gameName);
    const qualifies = scores.length < 10 || finalScore > (scores[scores.length - 1]?.score || 0);
    if (qualifies) showScoreEntry(gameName, finalScore);
  }

  function showScoreEntry(gameName, finalScore) {
    const root = document.getElementById('modal-root');
    if (!root) return;
    root.innerHTML = `
      <div class="modal-overlay" onclick="if(event.target===this)this.remove()">
        <div class="modal-box" style="text-align:center">
          <div class="modal-title">New High Score!</div>
          <div style="font-size:28px;color:var(--gold);font-family:var(--font-mono);margin:12px 0">${finalScore}</div>
          <div style="color:var(--text-muted);font-size:13px;margin-bottom:12px">Enter 3 characters:</div>
          <input class="score-input" id="score-initials" maxlength="3" autofocus
                 onkeydown="if(event.key==='Enter')submitHighScore('${gameName}',${finalScore})">
          <div class="modal-actions" style="justify-content:center">
            <button class="modal-btn primary" onclick="submitHighScore('${gameName}',${finalScore})">Save</button>
            <button class="modal-btn" onclick="document.getElementById('modal-root').innerHTML=''">Cancel</button>
          </div>
        </div>
      </div>`;
    setTimeout(() => {
      const inp = document.getElementById('score-initials');
      if (inp) inp.focus();
    }, 100);
  }

  function renderScoreboard(gameName) {
    const el = document.getElementById('scoreboard-entries');
    if (!el) return;
    const scores = getScores(gameName || currentGameName);
    if (scores.length === 0) {
      el.innerHTML = '<div style="font-size:10px;color:var(--text-faint);text-align:center;padding:4px">No scores yet</div>';
      return;
    }
    el.innerHTML = scores.map((s, i) =>
      `<div class="scoreboard-entry">
        <span class="rank">#${i+1}</span>
        <span class="initials">${s.name || '???'}</span>
        <span>${s.score}</span>
      </div>`
    ).join('');
  }

  return { init, start, stop, getScores, saveScores, checkHighScore, renderScoreboard, currentGameName: () => currentGameName };
})();

function switchGame(name, btn) {
  document.querySelectorAll('.game-tab').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  // Socials is a non-game tab: it takes over the whole panel area.
  if (name === 'socials') {
    GameManager.stop();
    if (window.socialsOnTab) window.socialsOnTab(true);
    Haptic.vibrate(Haptic.PATTERNS.toggle);
    return;
  }
  if (window.socialsOnTab) window.socialsOnTab(false);
  GameManager.start(name);
  Haptic.vibrate(Haptic.PATTERNS.toggle);
}

function closeGamePanel() {
  const panel = document.getElementById('oracle-panel');
  if (panel) panel.classList.remove('visible');
  GameManager.stop();
}

function clearScoreboard() {
  const name = GameManager.currentGameName();
  localStorage.removeItem(`oracle_scores_${name}`);
  GameManager.renderScoreboard(name);
}

function submitHighScore(gameName, score) {
  const inp = document.getElementById('score-initials');
  const name = (inp ? inp.value : '???').toUpperCase().padEnd(3, ' ').substring(0, 3);
  const scores = GameManager.getScores(gameName);
  scores.push({ name, score });
  scores.sort((a, b) => b.score - a.score);
  GameManager.saveScores(gameName, scores.slice(0, 10));
  GameManager.renderScoreboard(gameName);
  document.getElementById('modal-root').innerHTML = '';
}


// ================================================================
//  ASTEROIDS (delta-time based)
// ================================================================
const Asteroids = (() => {
  let W, H, ship, bullets, asteroids_, particles;
  let score, lives, gameOver, restartTimer, keys;
  const COLORS = { ship:'#38c9c4', bullet:'#f0a500', asteroid:'#4a7ab8', particle:'#f0a500', text:'#dde4f5', bg:'#000a1a' };
  const SPEED = 60;

  function init(canvas) { W = canvas.width; H = canvas.height; reset(); }

  function reset() {
    keys = {}; score = 0; lives = 3; gameOver = false; restartTimer = 0;
    bullets = []; particles = []; ship = createShip();
    asteroids_ = [];
    for (let i = 0; i < 5; i++) spawnAsteroid(3);
  }

  function createShip() {
    return { x:W/2, y:H/2, vx:0, vy:0, angle:-Math.PI/2, thrusting:false, shootCooldown:0, invincible:120, blinkTimer:0 };
  }

  function spawnAsteroid(size, x, y) {
    let a = Math.random()*Math.PI*2, spd = (4-size)*0.6+Math.random();
    let px = x !== undefined ? x : (Math.random()<0.5 ? Math.random()*60 : W-Math.random()*60);
    let py = y !== undefined ? y : (Math.random()<0.5 ? Math.random()*60 : H-Math.random()*60);
    let r = size*14, pts = [], n = 8+Math.floor(Math.random()*4);
    for (let i=0;i<n;i++) { let ang=(i/n)*Math.PI*2, rd=r*(0.75+Math.random()*0.5); pts.push({x:Math.cos(ang)*rd,y:Math.sin(ang)*rd}); }
    asteroids_.push({x:px,y:py,vx:Math.cos(a)*spd,vy:Math.sin(a)*spd,size,r,pts});
  }

  function update(dt) {
    const f = dt * SPEED;
    if (gameOver) { restartTimer += f; if (restartTimer > 180) reset(); return score; }

    if (keys["ArrowLeft"])  ship.angle -= 0.04 * f;
    if (keys["ArrowRight"]) ship.angle += 0.04 * f;
    if (keys['ArrowUp']) { ship.vx += Math.cos(ship.angle)*0.18*f; ship.vy += Math.sin(ship.angle)*0.18*f; ship.thrusting = true; }
    else ship.thrusting = false;

    ship.vx *= Math.pow(0.982, f); ship.vy *= Math.pow(0.982, f);
    ship.x = wrap(ship.x + ship.vx*f, W); ship.y = wrap(ship.y + ship.vy*f, H);
    ship.shootCooldown = Math.max(0, ship.shootCooldown - f);
    if (ship.invincible > 0) ship.invincible -= f;

    bullets = bullets.filter(b => b.life > 0);
    bullets.forEach(b => { b.x = wrap(b.x+b.vx*f,W); b.y = wrap(b.y+b.vy*f,H); b.life -= f; });

    asteroids_.forEach(a => { a.x = wrap(a.x+a.vx*f,W); a.y = wrap(a.y+a.vy*f,H); });

    particles = particles.filter(p => p.life > 0);
    particles.forEach(p => { p.x+=p.vx*f; p.y+=p.vy*f; p.vx*=Math.pow(0.95,f); p.vy*=Math.pow(0.95,f); p.life-=f; });

    for (let bi=bullets.length-1; bi>=0; bi--) {
      for (let ai=asteroids_.length-1; ai>=0; ai--) {
        let b=bullets[bi], a=asteroids_[ai];
        if (dist(b.x,b.y,a.x,a.y) < a.r) {
          bullets.splice(bi,1); explode(a);
          if (a.size>1) { spawnAsteroid(a.size-1,a.x,a.y); spawnAsteroid(a.size-1,a.x,a.y); }
          asteroids_.splice(ai,1); score += (4-a.size)*20;
          Haptic.vibrate(Haptic.PATTERNS.gameScore);
          if (asteroids_.length===0) { for (let i=0;i<5+Math.floor(score/400);i++) spawnAsteroid(3); }
          break;
        }
      }
    }

    if (ship.invincible <= 0) {
      for (let ai=asteroids_.length-1; ai>=0; ai--) {
        if (dist(ship.x,ship.y,asteroids_[ai].x,asteroids_[ai].y) < asteroids_[ai].r*0.7) {
          explode({x:ship.x,y:ship.y,size:2,r:20}); lives--;
          Haptic.vibrate(Haptic.PATTERNS.gameDie);
          if (lives<=0) { gameOver=true; GameManager.checkHighScore('asteroids', score); return score; }
          ship = createShip(); ship.invincible = 180; break;
        }
      }
    }
    return score;
  }

  function explode(obj) {
    for (let i=0;i<obj.size*6;i++) { let a=Math.random()*Math.PI*2, s=Math.random()*2+0.5;
      particles.push({x:obj.x,y:obj.y,vx:Math.cos(a)*s,vy:Math.sin(a)*s,life:30+Math.random()*20}); }
  }

  function draw(ctx) {
    ctx.fillStyle = COLORS.bg; ctx.fillRect(0,0,W,H);
    ctx.fillStyle='rgba(255,255,255,0.15)';
    [[30,40],[90,80],[150,30],[200,90],[260,50],[50,150],[220,180],[80,260],[180,240],[240,300]].forEach(([x,y])=>{ctx.fillRect(x,y,1,1);});

    asteroids_.forEach(a => {
      ctx.save(); ctx.translate(a.x,a.y); ctx.strokeStyle=COLORS.asteroid; ctx.lineWidth=1.5;
      ctx.beginPath(); a.pts.forEach((p,i)=>i===0?ctx.moveTo(p.x,p.y):ctx.lineTo(p.x,p.y));
      ctx.closePath(); ctx.stroke(); ctx.restore();
    });

    particles.forEach(p => { ctx.fillStyle=`rgba(240,165,0,${p.life/50})`; ctx.fillRect(p.x-1,p.y-1,2,2); });

    bullets.forEach(b => {
      ctx.fillStyle=COLORS.bullet; ctx.shadowColor=COLORS.bullet; ctx.shadowBlur=6;
      ctx.beginPath(); ctx.arc(b.x,b.y,2,0,Math.PI*2); ctx.fill(); ctx.shadowBlur=0;
    });

    if (!gameOver && (ship.invincible<=0 || Math.floor(ship.invincible/6)%2===0)) {
      ctx.save(); ctx.translate(ship.x,ship.y); ctx.rotate(ship.angle);
      ctx.strokeStyle=COLORS.ship; ctx.lineWidth=1.5; ctx.shadowColor=COLORS.ship; ctx.shadowBlur=6;
      ctx.beginPath(); ctx.moveTo(14,0); ctx.lineTo(-8,-7); ctx.lineTo(-4,0); ctx.lineTo(-8,7); ctx.closePath(); ctx.stroke();
      if (ship.thrusting) { ctx.fillStyle='#ff8800'; ctx.beginPath(); ctx.moveTo(-4,-3); ctx.lineTo(-4,3); ctx.lineTo(-14-Math.random()*6,0); ctx.closePath(); ctx.fill(); }
      ctx.restore();
    }

    ctx.fillStyle=COLORS.text; ctx.font='11px "JetBrains Mono"'; ctx.fillText(`♥ ${lives}`,8,18);
    ctx.shadowBlur = 0;

    if (gameOver) {
      ctx.fillStyle='#f0a500'; ctx.font='bold 20px Cinzel, serif'; ctx.textAlign='center';
      ctx.fillText('GAME OVER',W/2,H/2-14);
      ctx.fillStyle=COLORS.text; ctx.font='13px "JetBrains Mono"'; ctx.fillText(`Score: ${score}`,W/2,H/2+12);
      ctx.font='11px Rajdhani, sans-serif'; ctx.fillStyle='#6b80a8'; ctx.fillText('Restarting…',W/2,H/2+32);
      ctx.textAlign='left';
    }
  }

  function keydown(key) {
    keys[key] = true;
    if (key==='Space' && !gameOver && ship.shootCooldown<=0) {
      bullets.push({x:ship.x+Math.cos(ship.angle)*16,y:ship.y+Math.sin(ship.angle)*16,
                    vx:Math.cos(ship.angle)*9+ship.vx,vy:Math.sin(ship.angle)*9+ship.vy,life:55});
      ship.shootCooldown = 10; Haptic.vibrate([8]);
    }
  }
  function keyup(key) { if (keys) keys[key] = false; }

  function wrap(v,max) { return ((v%max)+max)%max; }
  function dist(x1,y1,x2,y2) { return Math.hypot(x2-x1,y2-y1); }

  const controls = '↑  Thrust     ←→ Rotate\nSpace  Shoot';
  return { init, update, draw, keydown, keyup, controls };
})();


// ================================================================
//  SNAKE (tick-based with dt accumulator)
// ================================================================
const Snake = (() => {
  const CELL = 20;
  let cols, rows, W, H, snake, dir, nextDir, food, score, gameOver, tickAccum, TICK_INTERVAL;
  const COLORS = { bg:'#000a1a', grid:'rgba(30,48,80,0.4)', head:'#f0a500', body:'#38c9c4', food:'#ff6565', text:'#dde4f5' };

  function init(canvas) {
    W = canvas.width; H = canvas.height;
    cols = Math.floor(W/CELL); rows = Math.floor(H/CELL); reset();
  }

  function reset() {
    let mx=Math.floor(cols/2), my=Math.floor(rows/2);
    snake = [{x:mx,y:my},{x:mx-1,y:my},{x:mx-2,y:my}];
    dir = {x:1,y:0}; nextDir = {x:1,y:0};
    score = 0; gameOver = false;
    TICK_INTERVAL = 0.20;
    tickAccum = 0;
    placeFood();
  }

  function placeFood() {
    let pos;
    do { pos = {x:Math.floor(Math.random()*cols),y:Math.floor(Math.random()*rows)}; }
    while (snake.some(s => s.x===pos.x && s.y===pos.y));
    food = pos;
  }

  function update(dt) {
    if (gameOver) return score;
    tickAccum += dt;
    if (tickAccum < TICK_INTERVAL) return score;
    tickAccum = 0;

    dir = nextDir;
    let head = {x:snake[0].x+dir.x, y:snake[0].y+dir.y};
    if (head.x<0||head.x>=cols||head.y<0||head.y>=rows) { gameOver=true; Haptic.vibrate(Haptic.PATTERNS.gameDie); GameManager.checkHighScore('snake',score); return score; }
    if (snake.some(s=>s.x===head.x&&s.y===head.y)) { gameOver=true; Haptic.vibrate(Haptic.PATTERNS.gameDie); GameManager.checkHighScore('snake',score); return score; }
    snake.unshift(head);

    if (head.x===food.x && head.y===food.y) {
      score += 10;
      TICK_INTERVAL = Math.max(0.08, TICK_INTERVAL - 0.005);
      placeFood(); Haptic.vibrate(Haptic.PATTERNS.gameScore);
    } else snake.pop();
    return score;
  }

  function draw(ctx) {
    ctx.fillStyle=COLORS.bg; ctx.fillRect(0,0,W,H);
    ctx.strokeStyle=COLORS.grid; ctx.lineWidth=0.5;
    for (let x=0;x<=cols;x++){ctx.beginPath();ctx.moveTo(x*CELL,0);ctx.lineTo(x*CELL,H);ctx.stroke();}
    for (let y=0;y<=rows;y++){ctx.beginPath();ctx.moveTo(0,y*CELL);ctx.lineTo(W,y*CELL);ctx.stroke();}
    snake.forEach((seg,i)=>{
      ctx.fillStyle=i===0?COLORS.head:COLORS.body;
      ctx.shadowColor=i===0?COLORS.head:'transparent'; ctx.shadowBlur=i===0?8:0;
      ctx.fillRect(seg.x*CELL+1,seg.y*CELL+1,CELL-2,CELL-2);
    });
    ctx.shadowBlur=0;
    let pulse=0.7+0.3*Math.sin(Date.now()/200);
    ctx.fillStyle=COLORS.food; ctx.shadowColor=COLORS.food; ctx.shadowBlur=10*pulse;
    ctx.beginPath(); ctx.arc(food.x*CELL+CELL/2,food.y*CELL+CELL/2,(CELL/2-2)*pulse,0,Math.PI*2); ctx.fill();
    ctx.shadowBlur=0;
    if (gameOver) {
      ctx.fillStyle='rgba(0,10,26,0.75)'; ctx.fillRect(0,H/2-44,W,88);
      ctx.fillStyle='#f0a500'; ctx.font='bold 20px Cinzel, serif'; ctx.textAlign='center';
      ctx.fillText('GAME OVER',W/2,H/2-14);
      ctx.fillStyle=COLORS.text; ctx.font='13px "JetBrains Mono"'; ctx.fillText(`Score: ${score}`,W/2,H/2+12);
      ctx.font='11px Rajdhani, sans-serif'; ctx.fillStyle='#6b80a8'; ctx.fillText('Press any arrow to restart',W/2,H/2+32);
      ctx.textAlign='left';
    }
  }

  function keydown(key) {
    const DIR_MAP = {ArrowUp:{x:0,y:-1},ArrowDown:{x:0,y:1},ArrowLeft:{x:-1,y:0},ArrowRight:{x:1,y:0}};
    const d = DIR_MAP[key]; if (!d) return;
    if (d.x===-dir.x && d.y===-dir.y) return;
    nextDir = d;
    if (gameOver) reset();
  }

  const controls = '↑↓←→  Move snake\nEat the red food!';
  return { init, update, draw, keydown, controls };
})();


// ================================================================
//  TIC-TAC-TOE
// ================================================================
const TicTacToe = (() => {
  let W, H, CELL, board, playerTurn, gameOver, result, score, statusMsg, moveTimeout;
  const COLORS = { bg:'#000a1a', grid:'#1e3050', x:'#38c9c4', o:'#f0a500', win:'#5bcf77', text:'#dde4f5', muted:'#6b80a8' };
  const WIN_LINES = [[0,1,2],[3,4,5],[6,7,8],[0,3,6],[1,4,7],[2,5,8],[0,4,8],[2,4,6]];

  function init(canvas) { W=canvas.width; H=canvas.height; CELL=Math.min(W,H)*0.26; score=0; reset(); }

  function reset() {
    board=Array(9).fill(null); playerTurn=true; gameOver=false; result=null;
    statusMsg='Your turn — click to place X'; clearTimeout(moveTimeout);
  }

  function update() { return score; }

  function draw(ctx) {
    ctx.fillStyle=COLORS.bg; ctx.fillRect(0,0,W,H);
    let ox=(W-CELL*3)/2, oy=(H-CELL*3)/2-20;
    ctx.strokeStyle=COLORS.grid; ctx.lineWidth=2;
    for (let i=1;i<3;i++){
      ctx.beginPath();ctx.moveTo(ox+i*CELL,oy);ctx.lineTo(ox+i*CELL,oy+3*CELL);ctx.stroke();
      ctx.beginPath();ctx.moveTo(ox,oy+i*CELL);ctx.lineTo(ox+3*CELL,oy+i*CELL);ctx.stroke();
    }
    board.forEach((cell,i)=>{
      if (!cell) return;
      let cx=ox+(i%3)*CELL+CELL/2, cy=oy+Math.floor(i/3)*CELL+CELL/2, r=CELL*0.3;
      if (cell==='X'){
        ctx.strokeStyle=COLORS.x; ctx.shadowColor=COLORS.x; ctx.shadowBlur=10; ctx.lineWidth=3;
        ctx.beginPath();ctx.moveTo(cx-r,cy-r);ctx.lineTo(cx+r,cy+r);ctx.stroke();
        ctx.beginPath();ctx.moveTo(cx+r,cy-r);ctx.lineTo(cx-r,cy+r);ctx.stroke();
      } else {
        ctx.strokeStyle=COLORS.o; ctx.shadowColor=COLORS.o; ctx.shadowBlur=10; ctx.lineWidth=3;
        ctx.beginPath();ctx.arc(cx,cy,r,0,Math.PI*2);ctx.stroke();
      }
      ctx.shadowBlur=0;
    });
    if (result && result.line) {
      let [a,,c]=result.line;
      ctx.strokeStyle=COLORS.win; ctx.lineWidth=3; ctx.shadowColor=COLORS.win; ctx.shadowBlur=12;
      ctx.beginPath();ctx.moveTo(ox+(a%3)*CELL+CELL/2,oy+Math.floor(a/3)*CELL+CELL/2);
      ctx.lineTo(ox+(c%3)*CELL+CELL/2,oy+Math.floor(c/3)*CELL+CELL/2);ctx.stroke(); ctx.shadowBlur=0;
    }
    ctx.fillStyle=COLORS.text; ctx.font='13px Rajdhani, sans-serif'; ctx.textAlign='center';
    ctx.fillText(statusMsg,W/2,oy-14);
    ctx.fillStyle=COLORS.muted; ctx.font='11px "JetBrains Mono"'; ctx.fillText(`Wins: ${score}`,W/2,H-14);
    ctx.textAlign='left';
  }

  function click(cx,cy) {
    if (!playerTurn||gameOver) { if (gameOver) reset(); return; }
    let ox=(W-CELL*3)/2, oy=(H-CELL*3)/2-20;
    let col=Math.floor((cx-ox)/CELL), row=Math.floor((cy-oy)/CELL);
    if (col<0||col>2||row<0||row>2) return;
    let idx=row*3+col; if (board[idx]) return;
    board[idx]='X'; Haptic.vibrate([12]);
    let res=checkWin(board);
    if (res) { endGame(res); return; }
    if (board.every(c=>c)) { endGame(null); return; }
    playerTurn=false; statusMsg='Oracle is thinking…';
    moveTimeout=setTimeout(aiMove,380+Math.random()*300);
  }

  function aiMove() {
    let move=getBestMove(); if (move===-1) return;
    board[move]='O';
    let res=checkWin(board);
    if (res) { endGame(res); return; }
    if (board.every(c=>c)) { endGame(null); return; }
    playerTurn=true; statusMsg='Your turn — click to place X';
  }

  function getBestMove() {
    for (let i=0;i<9;i++){if(!board[i]){board[i]='O';if(checkWin(board)){board[i]=null;return i;}board[i]=null;}}
    for (let i=0;i<9;i++){if(!board[i]){board[i]='X';if(checkWin(board)){board[i]=null;return i;}board[i]=null;}}
    for (let p of [4,0,2,6,8,1,3,5,7]) if(!board[p]) return p;
    return -1;
  }

  function checkWin(b) {
    for (let [a,bb,c] of WIN_LINES) if(b[a]&&b[a]===b[bb]&&b[a]===b[c]) return {winner:b[a],line:[a,bb,c]};
    return null;
  }

  function endGame(res) {
    gameOver=true; result=res;
    if (!res) statusMsg="It's a draw! Click to replay";
    else if (res.winner==='X') { score++; statusMsg='You win! Click to replay'; Haptic.vibrate(Haptic.PATTERNS.done); GameManager.checkHighScore('tictactoe',score); }
    else { statusMsg='Oracle wins! Click to replay'; Haptic.vibrate(Haptic.PATTERNS.gameDie); }
  }

  function keydown() {}
  const controls = 'Click a cell to place X\nOracle plays O\nClick to restart';
  return { init, update, draw, click, keydown, controls };
})();
