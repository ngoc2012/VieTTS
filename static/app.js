// ---- Environment detection ----
const IS_EXTENSION = typeof chrome !== 'undefined' && chrome.runtime && !!chrome.runtime.id;
const BASE_URL_KEY = 'vieneu_base_url';

function getBaseUrl() {
  const inp = document.getElementById('inp-server-url');
  return inp ? inp.value.replace(/\/+$/, '') : '';
}

// Persist server URL on change (extension only)
if (IS_EXTENSION) {
  const inp = document.getElementById('inp-server-url');
  if (inp) {
    const saved = localStorage.getItem(BASE_URL_KEY);
    if (saved) inp.value = saved;
    inp.addEventListener('input', () => localStorage.setItem(BASE_URL_KEY, getBaseUrl()));
  }
}

// ---- State persistence via localStorage ----
const STORAGE_KEY = 'vieneu_state';
const JOBS_KEY = 'vieneu_jobs'; // {rowId: jobId, ...}

function saveState() {
  const rows = [];
  document.querySelectorAll('.text-row').forEach(row => {
    rows.push({ id: row.dataset.id, text: row.querySelector('textarea').value });
  });
  const state = {
    backbone: document.getElementById('sel-backbone').value,
    codec: document.getElementById('sel-codec').value,
    voice: document.getElementById('sel-voice').value,
    temperature: document.getElementById('inp-temp').value,
    tab: document.getElementById('panel-clone').classList.contains('active') ? 'clone' : 'preset',
    ref_text: document.getElementById('inp-ref-text').value,
    rows: rows,
  };
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}

function getSavedState() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {}; } catch { return {}; }
}

function saveJobMap(map) { localStorage.setItem(JOBS_KEY, JSON.stringify(map)); }
function getJobMap() {
  try { return JSON.parse(localStorage.getItem(JOBS_KEY)) || {}; } catch { return {}; }
}

// Auto-save on any input change
document.addEventListener('input', saveState);
document.addEventListener('change', saveState);

// ---- Rows ----
let rowCounter = 0;
const pollTimers = {};       // rowId -> intervalId
const streamAborts = {};     // rowId -> AbortController (for PCM stream fetch)

function addRow(text, rowId) {
  if (!rowId) rowId = 'r' + (++rowCounter);
  else { const n = parseInt(rowId.slice(1)); if (n >= rowCounter) rowCounter = n; }
  const container = document.getElementById('text-rows');
  const div = document.createElement('div');
  div.className = 'text-row';
  div.dataset.id = rowId;
  div.innerHTML = `
    <div class="text-row-input">
      <textarea rows="2" placeholder="Nhập văn bản tiếng Việt...">${esc(text || '')}</textarea>
      <div class="row-btns">
        <button class="btn-clear" data-action="clear">Clear</button>
        <button class="btn-success row-gen" data-action="gen">Gen</button>
        <button class="btn-stop" data-action="stop">Stop</button>
      </div>
    </div>
    <div class="row-result">
      <div class="status" data-role="status"></div>
      <audio controls style="display:none" data-role="player"></audio>
    </div>`;
  container.appendChild(div);
  saveState();
  return rowId;
}

function stopStream(rowId) {
  if (streamAborts[rowId]) {
    streamAborts[rowId].abort();
    delete streamAborts[rowId];
  }
  removeFromPlayQueue(rowId);
  const el = getRowEl(rowId);
  if (el) {
    el.player.onended = null;
    el.player.pause();
    if (el.player.src && el.player.src.startsWith('blob:')) {
      URL.revokeObjectURL(el.player.src);
    }
  }
}

function stopRow(rowId) {
  stopStream(rowId);
  cancelFromQueue(rowId);
  if (pollTimers[rowId]) { clearInterval(pollTimers[rowId]); delete pollTimers[rowId]; }
  // Cancel server-side generation
  const jobMap = getJobMap();
  const jobId = jobMap[rowId];
  if (jobId) {
    fetch(`${getBaseUrl()}/api/cancel/${jobId}`, { method: 'POST' }).catch(() => {});
  }
  const el = getRowEl(rowId);
  if (el) {
    el.btn.disabled = false;
    setStatus(el.st, 'info', 'Stopped');
  }
}

function clearRow(rowId) {
  stopStream(rowId);
  cancelFromQueue(rowId);
  const allRows = document.querySelectorAll('.text-row');
  if (allRows.length > 1) {
    // Remove this row entirely
    const row = document.querySelector(`.text-row[data-id="${rowId}"]`);
    if (row) row.remove();
    if (pollTimers[rowId]) { clearInterval(pollTimers[rowId]); delete pollTimers[rowId]; }
    const jm = getJobMap(); delete jm[rowId]; saveJobMap(jm);
  } else {
    // Last row — just clear content
    const el = getRowEl(rowId);
    if (!el) return;
    el.textarea.value = '';
    el.st.className = 'status'; el.st.textContent = '';
    el.player.style.display = 'none'; el.player.removeAttribute('src');
  }
  saveState();
}

function stopAll() {
  const jobMap = getJobMap();
  for (const id of Object.keys(pollTimers)) { clearInterval(pollTimers[id]); delete pollTimers[id]; }
  for (const id of Object.keys(streamAborts)) stopStream(id);
  genQueue.length = 0;
  stopGenQueuePoller();
  playQueue = []; activePlayer = null;
  // Cancel all server-side jobs
  for (const [rowId, jobId] of Object.entries(jobMap)) {
    fetch(`${getBaseUrl()}/api/cancel/${jobId}`, { method: 'POST' }).catch(() => {});
  }
  // Re-enable all Gen buttons and show Stopped status
  document.querySelectorAll('.text-row').forEach(row => {
    const rowId = row.dataset.id;
    const el = getRowEl(rowId);
    if (el) {
      el.btn.disabled = false;
      if (el.st.classList.contains('info')) setStatus(el.st, 'info', 'Stopped');
    }
  });
  document.getElementById('btn-gen-all').disabled = false;
}

function clearAll() {
  // Stop all poll timers, streams, queue, and playback queue
  for (const id of Object.keys(pollTimers)) { clearInterval(pollTimers[id]); delete pollTimers[id]; }
  for (const id of Object.keys(streamAborts)) stopStream(id);
  genQueue.length = 0;
  stopGenQueuePoller();
  playQueue = []; activePlayer = null;
  // Remove all rows except keep one empty
  document.getElementById('text-rows').innerHTML = '';
  saveJobMap({});
  addRow('');
}

function downloadAll() {
  const jobMap = getJobMap();
  let i = 0;
  for (const [rowId, jobId] of Object.entries(jobMap)) {
    i++;
    setTimeout(() => {
      const a = document.createElement('a');
      a.href = `${getBaseUrl()}/api/audio/${jobId}`;
      a.download = `vieneu_${rowId}.wav`;
      document.body.appendChild(a);
      a.click();
      a.remove();
    }, i * 500);
  }
}


const LETTER_MAP = {
  A: "Ây",
  B: "Bi",
  C: "Xi",
  D: "Đi",
  E: "I",
  F: "Ép",
  G: "Gi",
  H: "Hát",
  I: "Ai",
  J: "Giây",
  K: "Kây",
  L: "En",
  M: "Em",
  N: "En",
  O: "Âu",
  P: "Pi",
  Q: "Kiu",
  R: "A",
  S: "Ét",
  T: "Ti",
  U: "Iu",
  V: "Vi",
  W: "Đáp-bờ-liu",
  X: "Ích",
  Y: "Oai",
  Z: "Dét"
};

const ENGLISH_ABBR_WHITELIST = new Set([

// ===== Currency =====
"USD","EUR","GBP","JPY","KRW","CNY","RMB","AUD","CAD","CHF","SGD","HKD","THB","VND",
"INR","MYR","IDR","PHP","NZD","SEK","NOK","DKK","RUB","TRY","ZAR","BRL","MXN",

// ===== Countries / Global =====
"USA","UK","EU","UAE","UN","UNICEF","UNESCO","WHO","WTO","IMF","WB",
"NATO","ASEAN","APEC","OPEC","OECD","G7","G20","BRICS",

// ===== Finance =====
"GDP","GNP","CPI","PPI","ROI","ROE","ROA","EPS","EBIT","EBITDA","NAV","IRR",
"IPO","SEO","MNA","MNC","SME","LLC","PLC","JSC","OTC","ETF","REIT",
"FOREX","FX","P2P","POS","APR","APY","LTV","CAC","ARPU","AUM",
"BT", "BOT",

// ===== Corporate Roles =====
"CEO","CFO","CTO","COO","CMO","CIO","CDO","CHRO","CSO","CISO","CAO","CPO",

// ===== Business Models =====
"B2B","B2C","B2G","C2C","D2C","B2E",
"SAAS","PAAS","IAAS","DAAS","FAAS",

// ===== Startup / Product =====
"MVP","PMF","OKR","KPI","NPS","CSAT","TAM","SAM","SOM",

// ===== Tech Core =====
"IT","ICT","AI","ML","DL","NLP","LLM","CV","AR","VR","XR",
"IOT","IIOT","API","SDK","IDE","CLI","GUI","SDK",
"CPU","GPU","TPU","NPU","RAM","ROM","SSD","HDD","NVME",

// ===== Web / Internet =====
"HTML","CSS","JS","TS","PHP","SQL","JSON","XML","YAML",
"HTTP","HTTPS","FTP","SSH","SSL","TLS","URL","URI","CDN",
"DNS","TCP","UDP","IP","IPV4","IPV6","SMTP","POP3","IMAP",

// ===== Programming / Dev =====
"OOP","MVC","MVVM","ORM","JWT","OAUTH","REST","SOAP","GRAPHQL",
"CRUD","CI","CD","TDD","BDD","DDD","SOLID","DRY","KISS",
"SDK","SLA","SRE","QA","QC",

// ===== Cloud =====
"AWS","GCP","AZURE","EC2","S3","RDS","IAM","VPC","EKS","ECS",
"CDN","DNS","LB","WAF",

// ===== DevOps =====
"CI/CD","K8S","VM","VPS","CDN","IAC","SRE","ELK","APM",

// ===== Security =====
"OTP","MFA","2FA","PIN","CVV","AES","RSA","SHA","MD5",
"DDoS","XSS","CSRF","SQLI","IDS","IPS","SIEM","PKI",

// ===== Mobile =====
"SMS","MMS","SIM","ESIM","GPS","APK","IPA","UI","UX","OTA",

// ===== Telecom =====
"4G","5G","LTE","VoIP","ISP","LAN","WAN","VPN","NAT",

// ===== Data / AI =====
"ETL","ELT","BI","OLAP","OLTP","EDA","CNN","RNN","GAN",
"LLM","GPT","BERT","SVM","KNN","RL","PCA",

// ===== Big Tech =====
"IBM","HP","AMD","INTEL","NVIDIA","META","MSFT","GOOG","AAPL","TSLA",

// ===== Blockchain / Crypto =====
"BTC","ETH","USDT","NFT","DAO","DEX","CEX","WEB3","ICO","IDO","IEO",

// ===== Media =====
"TV","PR","SEO","SEM","KOL","KOC","UGC","CTR","CPM","CPC","CPA",

// ===== Education =====
"MBA","PHD","BA","MA","TOEIC","IELTS","SAT","ACT","GPA","MOOC",

// ===== Medical =====
"HIV","AIDS","PCR","DNA","RNA","BMI","ICU","WHO","CDC","FDA",

// ===== Logistics =====
"COD","FOB","CIF","SKU","OEM","ODM","3PL","4PL",

// ===== Game =====
"RPG","FPS","MMO","PVP","PVE","NPC","XP","DLC","AAA",

// ===== Common Abbrev =====
"FAQ","DIY","ASAP","ETA","TBD","FYI","AKA","BTW","OMG","IDK","IMO",

// ===== Enterprise =====
"ERP","CRM","HRM","SCM","BPM","RPA","KMS","DMS",

// ===== Banking =====
"ATM","SWIFT","IBAN","SEPA","KYC","AML","POS","QR","EMV",

// ===== Hardware =====
"HDMI","USB","PCI","SATA","OLED","LCD","LED","IPS","FPS",

// ===== Misc Technical =====
"API","SDK","CLI","GUI","FTP","SSH","SSL","TLS"

]);

// ---- Custom whitelist persistence ----
const WHITELIST_KEY = 'vieneu_custom_whitelist';

function getCustomWhitelist() {
  try { return JSON.parse(localStorage.getItem(WHITELIST_KEY)) || []; } catch { return []; }
}

function parseWhitelistInput(text) {
  return text.split(/[,;\s]+/).map(w => w.trim().toUpperCase()).filter(w => w.length > 0);
}

function applyCustomWhitelist(words) {
  words.forEach(w => ENGLISH_ABBR_WHITELIST.add(w));
}

function saveWhitelist() {
  const inp = document.getElementById('inp-whitelist');
  const words = parseWhitelistInput(inp.value);
  localStorage.setItem(WHITELIST_KEY, JSON.stringify(words));
  applyCustomWhitelist(words);
  inp.value = words.join(', ');
  const st = document.getElementById('whitelist-status');
  setStatus(st, 'success', `Saved ${words.length} custom abbreviation${words.length !== 1 ? 's' : ''}`);
}

// Load saved custom words on startup
applyCustomWhitelist(getCustomWhitelist());


const VIET_ABBREVIATION_MAP = {

  // ===== Nhà nước - Chính phủ =====
  UBND: "Ủy Ban Nhân Dân",
  HĐND: "Hội Đồng Nhân Dân",
  QH: "Quốc Hội",
  CP: "Chính Phủ",
  TAND: "Tòa Án Nhân Dân",
  VKSND: "Viện Kiểm Sát Nhân Dân",
  BCA: "Bộ Công An",
  BQP: "Bộ Quốc Phòng",
  BGDĐT: "Bộ Giáo Dục Và Đào Tạo",
  BYT: "Bộ Y Tế",
  BCT: "Bộ Công Thương",
  BTC: "Bộ Tài Chính",
  BTNMT: "Bộ Tài Nguyên Và Môi Trường",
  BTTTT: "Bộ Thông Tin Và Truyền Thông",
  BKHĐT: "Bộ Kế Hoạch Và Đầu Tư",
  UBMTTQ: "Ủy Ban Mặt Trận Tổ Quốc",
  UBATGTQG: "Ủy Ban An Toàn Giao Thông Quốc Gia",
  CHXHCN: "Cộng Hòa Xã Hội Chủ Nghĩa",
  CHXHCNVN: "Cộng Hòa Xã Hội Chủ Nghĩa Việt Nam",

  // ===== Công an - Pháp luật =====
  CSGT: "Cảnh Sát Giao Thông",
  CSCĐ: "Cảnh Sát Cơ Động",
  PCCC: "Phòng Cháy Chữa Cháy",
  ATGT: "An Toàn Giao Thông",
  TNGT: "Tai Nạn Giao Thông",
  ANTT: "An Ninh Trật Tự",
  VPHC: "Vi Phạm Hành Chính",
  XLVP: "Xử Lý Vi Phạm",

  // ===== Y tế =====
  BV: "Bệnh Viện",
  PK: "Phòng Khám",
  BHYT: "Bảo Hiểm Y Tế",
  BHXH: "Bảo Hiểm Xã Hội",
  BHTN: "Bảo Hiểm Thất Nghiệp",
  ATTP: "An Toàn Thực Phẩm",
  VSATTP: "Vệ Sinh An Toàn Thực Phẩm",
  CDC: "Trung Tâm Kiểm Soát Bệnh Tật",

  // ===== Giáo dục =====
  ĐH: "Đại Học",
  CĐ: "Cao Đẳng",
  THPT: "Trung Học Phổ Thông",
  THCS: "Trung Học Cơ Sở",
  TH: "Tiểu Học",
  GDTX: "Giáo Dục Thường Xuyên",
  HS: "Học Sinh",
  SV: "Sinh Viên",
  GV: "Giáo Viên",

  // ===== Địa lý - Hành chính =====
  TP: "Thành Phố",
  TPHCM: "Thành Phố Hồ Chí Minh",
  "TP.HCM": "Thành Phố Hồ Chí Minh",
  HCM: "Hồ Chí Minh",
  HN: "Hà Nội",
  Q: "Quận",
  H: "Huyện",
  TX: "Thị Xã",
  P: "Phường",
  X: "Xã",
  VN: "Việt Nam",

  // ===== Kinh tế - Tài chính =====
  NHNN: "Ngân Hàng Nhà Nước",
  NHTM: "Ngân Hàng Thương Mại",
  DN: "Doanh Nghiệp",
  DNNN: "Doanh Nghiệp Nhà Nước",
  FDI: "Đầu Tư Trực Tiếp Nước Ngoài",
  GDP: "Tổng Sản Phẩm Quốc Nội",
  BĐS: "Bất Động Sản",
  CK: "Chứng Khoán",
  TTCK: "Thị Trường Chứng Khoán",

  // ===== Giấy tờ - Cá nhân =====
  CMND: "Chứng Minh Nhân Dân",
  CCCD: "Căn Cước Công Dân",
  GPLX: "Giấy Phép Lái Xe",
  MST: "Mã Số Thuế",
  MSHS: "Mã Số Học Sinh",

  // ===== Công nghệ - Truyền thông =====
  CNTT: "Công Nghệ Thông Tin",
  TMĐT: "Thương Mại Điện Tử",
  MXH: "Mạng Xã Hội",
  CSDL: "Cơ Sở Dữ Liệu",
  HTTT: "Hệ Thống Thông Tin",
  TTS: "Chuyển Văn Bản Thành Giọng Nói",

  // ===== Giao thông - Hạ tầng =====
  // BOT: "Xây Dựng Vận Hành Chuyển Giao",
  // BT: "Xây Dựng Chuyển Giao",
  GTVT: "Giao Thông Vận Tải",
  ĐTNĐ: "Đường Thủy Nội Địa",
  ĐSVN: "Đường Sắt Việt Nam"
};

// ---- Custom Viet abbreviation persistence ----
const VIETABBR_KEY = 'vieneu_custom_vietabbr';

function getCustomVietAbbr() {
  try { return JSON.parse(localStorage.getItem(VIETABBR_KEY)) || {}; } catch { return {}; }
}

function parseVietAbbrInput(text) {
  const map = {};
  for (const line of text.split('\n')) {
    const m = line.match(/^\s*([^:]+?)\s*:\s*"([^"]+)"\s*,?\s*$/);
    if (m) map[m[1].trim().toUpperCase()] = m[2].trim();
  }
  return map;
}

function formatVietAbbrMap(map) {
  return Object.entries(map).map(([k, v]) => `${k}: "${v}"`).join('\n');
}

function applyCustomVietAbbr(map) {
  Object.assign(VIET_ABBREVIATION_MAP, map);
}

function saveVietAbbr() {
  const inp = document.getElementById('inp-vietabbr');
  const map = parseVietAbbrInput(inp.value);
  localStorage.setItem(VIETABBR_KEY, JSON.stringify(map));
  applyCustomVietAbbr(map);
  inp.value = formatVietAbbrMap(map);
  const count = Object.keys(map).length;
  const st = document.getElementById('vietabbr-status');
  setStatus(st, 'success', `Saved ${count} custom abbreviation${count !== 1 ? 's' : ''}`);
}

// Load saved custom Viet abbreviations on startup
applyCustomVietAbbr(getCustomVietAbbr());


function convertVietnameseAbbreviation(text) {
  return text.replace(/\b[0-9A-ZĐ\.]{2,}\b/gu, (word) => {
    const normalized = word.replace(/\./g, '');
    return VIET_ABBREVIATION_MAP[normalized] || word;
  });
}

function convertEnglishAbbreviation(text) {
  return text.replace(/\b[A-Za-z0-9]{2,}\b/g, (word) => {
    if (!ENGLISH_ABBR_WHITELIST.has(word)) return word;

    return word
      .split('')
      .map(c => LETTER_MAP[c] || c)
      .join(' ');
  });
}

function preprocessText(text) {
  // Remove URLs, hashtags, emoji, repeated special chars
  text = text.replace(/https?:\/\/\S+/gi, '');
  text = text.replace(/#\S+/g, '');
  text = text.replace(/[\u{1F600}-\u{1F64F}\u{1F300}-\u{1F5FF}\u{1F680}-\u{1F6FF}\u{1F1E0}-\u{1F1FF}\u{1F900}-\u{1F9FF}\u{1FA00}-\u{1FA6F}\u{1FA70}-\u{1FAFF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{FE00}-\u{FE0F}\u{200D}\u{20E3}\u{E0020}-\u{E007F}]/gu, '');
  text = text.replace(/([^a-zA-Z0-9\s\u{00C0}-\u{024F}\u{1E00}-\u{1EFF}])\1{2,}/gu, '');
  text = text.replace(/[ \t]+/g, ' ');
  text = text.replace(/\n\s*\n+/g, '\n');

  // VN abbreviation first
  text = convertVietnameseAbbreviation(text);

  // Convert English abbreviations to Vietnamese phonetics
  text = convertEnglishAbbreviation(text);

  return text.trim();
}

function getRowEl(rowId) {
  const row = document.querySelector(`.text-row[data-id="${rowId}"]`);
  if (!row) return null;
  return {
    row,
    textarea: row.querySelector('textarea'),
    btn: row.querySelector('.row-gen'),
    st: row.querySelector('[data-role="status"]'),
    player: row.querySelector('[data-role="player"]'),
  };
}

function setStatus(el, cls, msg) {
  el.className = 'status ' + cls;
  el.textContent = msg;
}

// ---- Playback queue: only one row plays at a time ----
let playQueue = [];       // rowIds waiting to play
let activePlayer = null;  // rowId currently playing
const pendingPlay = {};   // rowId -> function to call when it's this row's turn

function onPlayerFinished(rowId) {
  if (activePlayer === rowId) activePlayer = null;
  if (playQueue.length > 0) {
    const nextId = playQueue.shift();
    const fn = pendingPlay[nextId];
    if (fn) { delete pendingPlay[nextId]; fn(); }
  }
}

function requestPlay(rowId, playFn) {
  if (!activePlayer || activePlayer === rowId) {
    activePlayer = rowId;
    playFn();
  } else {
    if (!playQueue.includes(rowId)) playQueue.push(rowId);
    pendingPlay[rowId] = () => { activePlayer = rowId; playFn(); };
  }
}

function removeFromPlayQueue(rowId) {
  playQueue = playQueue.filter(id => id !== rowId);
  delete pendingPlay[rowId];
  if (activePlayer === rowId) activePlayer = null;
}

// ---- MediaSource streaming (WebM/Opus) ----
const MSE_MIME = 'audio/webm; codecs="opus"';
const MIN_BUFFER_SEC = 15.0;

async function startPcmStream(rowId, jobId) {
  stopStream(rowId);
  const el = getRowEl(rowId);
  if (!el) return;

  if (!window.MediaSource || !MediaSource.isTypeSupported(MSE_MIME)) {
    setStatus(el.st, 'error', 'Browser does not support MediaSource with WebM/Opus');
    return;
  }

  const abort = new AbortController();
  streamAborts[rowId] = abort;

  const mediaSource = new MediaSource();
  el.player.src = URL.createObjectURL(mediaSource);
  el.player.style.display = 'block';

  await new Promise(resolve => mediaSource.addEventListener('sourceopen', resolve, { once: true }));

  const sourceBuffer = mediaSource.addSourceBuffer(MSE_MIME);
  let totalBytes = 0;
  let playStarted = false;

  // When audio ends, switch to server WAV for lossless replay and advance queue
  el.player.onended = () => {
    const serverUrl = el.row.dataset.serverAudio;
    if (serverUrl) {
      if (el.player.src && el.player.src.startsWith('blob:')) URL.revokeObjectURL(el.player.src);
      el.player.src = serverUrl;
      el.player.onended = null;
    }
    onPlayerFinished(rowId);
  };

  async function waitForBuffer() {
    if (sourceBuffer.updating) {
      await new Promise(resolve => sourceBuffer.addEventListener('updateend', resolve, { once: true }));
    }
  }

  try {
    const resp = await fetch(`${getBaseUrl()}/api/stream/${jobId}`, { signal: abort.signal });
    if (!resp.ok || !resp.body) return;
    const reader = resp.body.getReader();

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      await waitForBuffer();
      try {
        sourceBuffer.appendBuffer(value);
      } catch (e) {
        console.error('SourceBuffer append error:', e);
        break;
      }
      totalBytes += value.length;
      await waitForBuffer();

      // Check buffered duration
      if (sourceBuffer.buffered.length > 0) {
        const bufferedEnd = sourceBuffer.buffered.end(0);
        const bufferedSec = bufferedEnd - el.player.currentTime;

        if (!playStarted) {
          setStatus(el.st, 'info',
            `Buffering ${bufferedSec.toFixed(1)}s / ${MIN_BUFFER_SEC.toFixed(1)}s — ${(totalBytes / 1024).toFixed(0)}KB`);

          if (bufferedSec >= MIN_BUFFER_SEC) {
            playStarted = true;
            setStatus(el.st, 'info', `Playing — buffered ${bufferedSec.toFixed(1)}s`);
            requestPlay(rowId, () => el.player.play().catch(() => {}));
          }
        }
      }
    }

    // Signal end of stream
    await waitForBuffer();
    if (mediaSource.readyState === 'open') {
      mediaSource.endOfStream();
    }

    // Very short audio — never hit buffer threshold
    if (!playStarted && sourceBuffer.buffered.length > 0) {
      playStarted = true;
      requestPlay(rowId, () => el.player.play().catch(() => {}));
    }

  } catch (e) {
    if (e.name !== 'AbortError') console.error('MSE stream error:', e);
  } finally {
    delete streamAborts[rowId];
  }
}

// ---- Init ----
const DEFAULT_BACKBONE = "VieNeu-TTS-0.3B-q4-gguf";
const DEFAULT_CODEC = "NeuCodec ONNX (Fast CPU)";
const DEFAULT_VOICE = "Binh";

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

async function init() {
  const saved = getSavedState();

  try {
    const [models, codecs] = await Promise.all([
      fetch(`${getBaseUrl()}/api/models`).then(r => r.json()),
      fetch(`${getBaseUrl()}/api/codecs`).then(r => r.json()),
    ]);

    const pickBackbone = saved.backbone || DEFAULT_BACKBONE;
    const selB = document.getElementById('sel-backbone');
    selB.innerHTML = models.map(m =>
      `<option value="${esc(m.name)}" title="${esc(m.description)}"${m.name === pickBackbone ? ' selected' : ''}>${esc(m.name)}</option>`
    ).join('');

    const pickCodec = saved.codec || DEFAULT_CODEC;
    const selC = document.getElementById('sel-codec');
    selC.innerHTML = codecs.map(c =>
      `<option value="${esc(c.name)}" title="${esc(c.description)}"${c.name === pickCodec ? ' selected' : ''}>${esc(c.name)}</option>`
    ).join('');

    const pickVoice = saved.voice || DEFAULT_VOICE;
    const voices = await fetch(`${getBaseUrl()}/api/voices`).then(r => r.json());
    const selV = document.getElementById('sel-voice');
    if (voices.length > 0) {
      selV.innerHTML = voices.map(v =>
        `<option value="${esc(v.id)}"${v.id === pickVoice ? ' selected' : ''}>${esc(v.description)} (${esc(v.id)})</option>`
      ).join('');
    }
    setStatus(document.getElementById('model-status'), 'success', 'Model preloaded and ready.');
  } catch (e) {
    setStatus(document.getElementById('model-status'), 'error', 'Cannot connect to server: ' + e.message);
  }

  if (saved.temperature) document.getElementById('inp-temp').value = saved.temperature;
  if (saved.ref_text) document.getElementById('inp-ref-text').value = saved.ref_text;
  if (saved.tab && saved.tab !== 'preset') switchTab(saved.tab);

  // Restore custom whitelist textarea
  const customWords = getCustomWhitelist();
  const inpWl = document.getElementById('inp-whitelist');
  if (inpWl && customWords.length > 0) inpWl.value = customWords.join(', ');

  // Restore custom Viet abbreviation textarea
  const customVa = getCustomVietAbbr();
  const inpVa = document.getElementById('inp-vietabbr');
  if (inpVa && Object.keys(customVa).length > 0) inpVa.value = formatVietAbbrMap(customVa);

  // Restore rows
  if (saved.rows && saved.rows.length > 0) {
    saved.rows.forEach(r => addRow(r.text, r.id));
  } else {
    addRow('');
  }

  // Restore jobs per row
  const jobMap = getJobMap();
  for (const [rowId, jobId] of Object.entries(jobMap)) {
    const el = getRowEl(rowId);
    if (!el) continue;
    try {
      const r = await fetch(`${getBaseUrl()}/api/status/${jobId}`);
      if (r.ok) {
        const data = await r.json();
        if (data.status === 'done') {
          setStatus(el.st, 'success', data.progress || 'Done!');
          el.player.src = `${getBaseUrl()}${data.audio_url}`;
          el.player.style.display = 'block';
        } else if (data.status === 'processing' || data.status === 'pending') {
          setStatus(el.st, 'info', 'Resuming...');
          pollRow(rowId, jobId);
        } else if (data.status === 'error') {
          setStatus(el.st, 'error', 'Error: ' + (data.error || 'Unknown'));
        }
      } else {
        delete jobMap[rowId]; saveJobMap(jobMap);
      }
    } catch { delete jobMap[rowId]; saveJobMap(jobMap); }
  }
}

// ---- Tabs ----
function switchTab(tab) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  document.getElementById('panel-preset').classList.toggle('active', tab === 'preset');
  document.getElementById('panel-clone').classList.toggle('active', tab === 'clone');
  const panelWl = document.getElementById('panel-whitelist');
  if (panelWl) panelWl.classList.toggle('active', tab === 'whitelist');
  const panelVa = document.getElementById('panel-vietabbr');
  if (panelVa) panelVa.classList.toggle('active', tab === 'vietabbr');
  saveState();
}

// ---- Load model ----
async function loadModel() {
  const btn = document.getElementById('btn-load');
  const st = document.getElementById('model-status');
  btn.disabled = true;
  setStatus(st, 'info', 'Loading model...');

  try {
    const resp = await fetch(`${getBaseUrl()}/api/load_model`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        backbone: document.getElementById('sel-backbone').value,
        codec: document.getElementById('sel-codec').value,
      }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || 'Failed');

    setStatus(st, 'success',
      `Model loaded: ${data.backbone} (${data.backbone_device}) + ${data.codec} (${data.codec_device})`);

    const voices = await fetch(`${getBaseUrl()}/api/voices`).then(r => r.json());
    const selV = document.getElementById('sel-voice');
    const savedVoice = getSavedState().voice || DEFAULT_VOICE;
    if (voices.length > 0) {
      selV.innerHTML = voices.map(v =>
        `<option value="${esc(v.id)}"${v.id === savedVoice ? ' selected' : ''}>${esc(v.description)} (${esc(v.id)})</option>`
      ).join('');
    } else {
      selV.innerHTML = '<option value="">No preset voices available</option>';
    }
    saveState();
  } catch (e) {
    setStatus(st, 'error', 'Error: ' + e.message);
  } finally {
    btn.disabled = false;
  }
}

// ---- Generation queue: one request at a time, top rows first ----
const genQueue = [];      // ordered list of rowIds waiting to be submitted
let genQueueTimer = null; // single poller that drives the queue

function cancelFromQueue(rowId) {
  const idx = genQueue.indexOf(rowId);
  if (idx !== -1) genQueue.splice(idx, 1);
}

async function submitSynthesize(rowId, text) {
  const presetActive = document.getElementById('panel-preset').classList.contains('active');
  let resp;
  if (presetActive) {
    resp = await fetch(`${getBaseUrl()}/api/synthesize`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        text: text,
        voice_id: document.getElementById('sel-voice').value,
        temperature: parseFloat(document.getElementById('inp-temp').value) || 1.0,
      }),
    });
  } else {
    const fd = new FormData();
    fd.append('text', text);
    fd.append('temperature', document.getElementById('inp-temp').value);
    fd.append('ref_text', document.getElementById('inp-ref-text').value);
    const fileInput = document.getElementById('inp-ref-audio');
    if (fileInput.files.length > 0) fd.append('ref_audio', fileInput.files[0]);
    resp = await fetch(`${getBaseUrl()}/api/synthesize`, { method: 'POST', body: fd });
  }
  return resp;
}

// Try to submit the next row in the queue
async function processGenQueue() {
  if (genQueue.length === 0) {
    stopGenQueuePoller();
    document.getElementById('btn-gen-all').disabled = false;
    return;
  }

  // Single check: is the server busy?
  try {
    const r = await fetch(`${getBaseUrl()}/api/busy`);
    const info = await r.json();
    if (info.busy) {
      // Update status for all queued rows
      genQueue.forEach((rid, i) => {
        const el = getRowEl(rid);
        if (el) setStatus(el.st, 'info', `Queued #${i + 1} — waiting for server...`);
      });
      return; // will retry on next tick
    }
  } catch (e) {
    return; // network error, retry next tick
  }

  // Server is free — submit the first row in queue
  const rowId = genQueue.shift();
  const el = getRowEl(rowId);
  if (!el) { processGenQueue(); return; }

  const text = el.textarea.value.trim();
  if (!text) { el.btn.disabled = false; processGenQueue(); return; }

  try {
    const resp = await submitSynthesize(rowId, text);
    const data = await resp.json();
    if (resp.status === 503 && data.busy) {
      // Race condition: put it back at the front
      genQueue.unshift(rowId);
      setStatus(el.st, 'info', 'Queued #1 — waiting for server...');
      return;
    }
    if (!resp.ok) throw new Error(data.error || 'Failed');

    const jobMap = getJobMap(); jobMap[rowId] = data.job_id; saveJobMap(jobMap);
    setStatus(el.st, 'info', 'Processing...');
    startPcmStream(rowId, data.job_id);
    pollRow(rowId, data.job_id);
  } catch (e) {
    setStatus(el.st, 'error', 'Error: ' + e.message);
    el.btn.disabled = false;
  }
}

function startGenQueuePoller() {
  if (genQueueTimer) return;
  processGenQueue(); // run immediately first
  genQueueTimer = setInterval(processGenQueue, 3000);
}

function stopGenQueuePoller() {
  if (genQueueTimer) { clearInterval(genQueueTimer); genQueueTimer = null; }
}

function enqueueRow(rowId) {
  const el = getRowEl(rowId);
  if (!el) return;
  stopStream(rowId);
  cancelFromQueue(rowId);
  el.btn.disabled = true;
  el.player.style.display = 'none';

  // Preprocess text before generating
  el.textarea.value = preprocessText(el.textarea.value);
  saveState();

  const text = el.textarea.value.trim();
  if (!text) { setStatus(el.st, 'error', 'Please enter text'); el.btn.disabled = false; return; }

  genQueue.push(rowId);
  const pos = genQueue.indexOf(rowId) + 1;
  setStatus(el.st, 'info', `Queued #${pos}...`);
  startGenQueuePoller();
}

// ---- Per-row generate ----
function generateRow(rowId) {
  enqueueRow(rowId);
}

// ---- Generate all rows ----
function generateAll() {
  const rows = document.querySelectorAll('.text-row');
  document.getElementById('btn-gen-all').disabled = true;
  for (const row of rows) {
    const rowId = row.dataset.id;
    const el = getRowEl(rowId);
    if (!el) continue;
    const text = el.textarea.value.trim();
    if (!text) continue;
    // Only enqueue if not already in queue or actively processing
    if (genQueue.indexOf(rowId) === -1) enqueueRow(rowId);
  }
}

function pollRow(rowId, jobId) {
  const el = getRowEl(rowId);
  if (!el) return;
  el.btn.disabled = true;

  if (pollTimers[rowId]) clearInterval(pollTimers[rowId]);
  pollTimers[rowId] = setInterval(async () => {
    const el = getRowEl(rowId);
    if (!el) { clearInterval(pollTimers[rowId]); delete pollTimers[rowId]; return; }
    try {
      const r = await fetch(`${getBaseUrl()}/api/status/${jobId}`);
      if (r.status === 404) {
        clearInterval(pollTimers[rowId]); delete pollTimers[rowId];
        const jm = getJobMap(); delete jm[rowId]; saveJobMap(jm);
        setStatus(el.st, 'error', 'Job expired (server may have restarted)');
        el.btn.disabled = false; return;
      }
      const data = await r.json();
      if (data.status === 'processing' || data.status === 'pending') {
        setStatus(el.st, 'info', data.progress || 'Processing...');
      } else if (data.status === 'done') {
        clearInterval(pollTimers[rowId]); delete pollTimers[rowId];
        setStatus(el.st, 'success', data.progress || 'Done!');
        el.btn.disabled = false;
        // Store server URL; onended handler will switch to it
        el.row.dataset.serverAudio = `${getBaseUrl()}${data.audio_url}`;
        // If no MSE stream active, set server WAV now
        if (el.player.paused && !(el.player.src && el.player.src.startsWith('blob:'))) {
          el.player.src = `${getBaseUrl()}${data.audio_url}`;
          el.player.style.display = 'block';
        }
      } else if (data.status === 'error') {
        clearInterval(pollTimers[rowId]); delete pollTimers[rowId];
        stopStream(rowId);
        setStatus(el.st, 'error', 'Error: ' + (data.error || 'Unknown'));
        el.btn.disabled = false;
      }
    } catch (e) {
      clearInterval(pollTimers[rowId]); delete pollTimers[rowId];
      setStatus(el.st, 'error', 'Polling error: ' + e.message);
      el.btn.disabled = false;
    }
  }, 1000);
}

// ---- Chrome extension: receive text from content script via chrome.storage ----
if (IS_EXTENSION) {
  function consumePendingTexts() {
    chrome.storage.local.get('pendingTexts', (result) => {
      const pending = result.pendingTexts || [];
      if (pending.length === 0) return;
      pending.forEach(text => insertTextToRow(text));
      chrome.storage.local.remove('pendingTexts');
    });
  }

  function insertTextToRow(text) {
    const rows = document.querySelectorAll('.text-row');
    const lastRow = rows[rows.length - 1];
    if (lastRow) {
      const textarea = lastRow.querySelector('textarea');
      if (textarea.value.trim()) {
        addRow(text);
      } else {
        textarea.value = text;
      }
    } else {
      addRow(text);
    }
    saveState();
  }

  consumePendingTexts();

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== 'local' || !changes.pendingTexts) return;
    const added = changes.pendingTexts.newValue || [];
    if (added.length === 0) return;
    added.forEach(text => insertTextToRow(text));
    chrome.storage.local.remove('pendingTexts');
  });
}

// ---- Chrome extension: inspect mode ----
function toggleInspect() {
  if (!IS_EXTENSION) return;
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (!tabs[0]) return;
    chrome.scripting.executeScript({
      target: { tabId: tabs[0].id },
      files: ['content.js'],
    });
  });
}

// ---- Bind all event listeners (no inline handlers — required for extension CSP) ----
document.getElementById('btn-load').addEventListener('click', loadModel);
document.getElementById('btn-add').addEventListener('click', () => addRow());
document.getElementById('btn-gen-all').addEventListener('click', generateAll);
document.getElementById('btn-download-all').addEventListener('click', downloadAll);
document.getElementById('btn-stop-all').addEventListener('click', stopAll);
document.getElementById('btn-clear-all').addEventListener('click', clearAll);

// Whitelist save button
const btnSaveWl = document.getElementById('btn-save-whitelist');
if (btnSaveWl) btnSaveWl.addEventListener('click', saveWhitelist);

// Viet abbreviation save button
const btnSaveVa = document.getElementById('btn-save-vietabbr');
if (btnSaveVa) btnSaveVa.addEventListener('click', saveVietAbbr);

// Inspect button (extension only)
const btnInspect = document.getElementById('btn-inspect');
if (btnInspect) btnInspect.addEventListener('click', toggleInspect);

// Tabs
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => switchTab(tab.dataset.tab));
});

// Event delegation for dynamic row buttons
document.getElementById('text-rows').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  const row = btn.closest('.text-row');
  if (!row) return;
  const rowId = row.dataset.id;
  const action = btn.dataset.action;
  if (action === 'clear') clearRow(rowId);
  else if (action === 'gen') generateRow(rowId);
  else if (action === 'stop') stopRow(rowId);
});

init();
