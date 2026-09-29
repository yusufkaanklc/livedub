# LiveDub — Canlı Çeviri ve Dublaj (Windows + macOS)

Bilgisayarda çalan sesi (YouTube, film, yayın, Zoom…) ya da mikrofonu **anlık olarak çevirir ve
seslendirir**. Çeviri, konuşma devam ederken akış hâlinde üretilir ve seçtiğiniz çıkışa (hoparlör,
kulaklık ya da toplantı uygulamasına giden sanal kablo) çalınır.

## Motorlar

| Motor | Nasıl çalışır | Gecikme | Çıkış dilleri | Gereken anahtar |
|---|---|---|---|---|
| **Gemini Live Translate** (`gemini-3.5-live-translate-preview`) — varsayılan | Konuşmacı konuşurken eşzamanlı çevirir, ses tonunu konuşmacıya uyarlar, zaten hedef dilde olan konuşmayı tekrar etmez | En düşük (konuşmayla birlikte akar) | 70+ dil, **Türkçe dahil** | Gemini ([ücretsiz al](https://aistudio.google.com/apikey)) |
| **OpenAI Realtime Translate** (`gpt-realtime-translate`) | Konuşmacı konuşurken eşzamanlı çevirir, ses tonunu konuşmacıya uyarlar | En düşük (konuşmayla birlikte akar) | en, es, pt, fr, ja, ru, zh, de, ko, hi, id, vi, it — **Türkçe yok** | OpenAI |
| **OpenAI Realtime GPT** (`gpt-realtime-1.5` vb.) | Tercüman olarak yönlendirilmiş konuşma modeli; her duraksamada çevirir | Düşük (duraksamadan sonra) | Tüm diller, **Türkçe dahil** | OpenAI |
| **Kaskad** | Deepgram canlı STT → OpenAI/DeepL çeviri → OpenAI/ElevenLabs TTS | Orta (cümle sonundan sonra) | Tüm diller, istediğiniz ses (ElevenLabs) | Deepgram + OpenAI/DeepL + OpenAI/ElevenLabs |

Özet: her yönde (Türkçeye ve Türkçeden) varsayılan motor **Gemini Live Translate**: eşzamanlı ve en
ucuzu. Ücretsiz katmanda ses girişi bedava, çeviri sesi ~0,018 $/dk, yani saatte ~1 $. Ücretsiz
katmanda gönderilen içerik Google tarafından ürün geliştirmede kullanılabilir. Karşılaştırma için OpenAI
Realtime Translate 0,034 $/dk'dır. Özel ses ya da klon ses istiyorsanız *Kaskad* motorunu seçin.

Fiyatlar Eylül 2026'daki resmi fiyat sayfalarından alınmıştır.

Gecikmeyi düşüren ayrıntılar:
- Ses 20 ms'lik bloklarla yakalanır, 40 ms'lik paketlerle WebSocket üzerinden akıtılır.
- Realtime GPT'de dublaj yeni konuşma başlayınca **kesilmez**; yanıtlar sıraya alınır. Kesintisiz
  konuşmada (video, yayın) en geç *En uzun parça* süresinde (varsayılan 7 sn) çeviri tetiklenir.
- Eski konuşma öğeleri silinir; oturum uzadıkça maliyet ve gecikme artmaz.
- Kaskad'da çeviri token token akar; ilk cümle bittiği anda TTS başlar. Kuyruk birikirse konuşma
  otomatik hızlanır.
- Bağlantı düşerse (veya sunucu oturum süresi dolarsa) otomatik yeniden bağlanır.
- Alt çubukta **Gecikme** (konuşma bitişi/başlangıcı → ilk dublaj sesi) ve **Kuyruk** (çalınmayı
  bekleyen dublaj) canlı gösterilir.

## Kurulum

LiveDub normal bir masaüstü uygulamasıdır. Python gerekmez.

- **Windows:** `dist/LiveDub/LiveDub.exe`. Klasörü istediğiniz yere taşıyabilirsiniz; `LiveDub.exe`
  için bir masaüstü kısayolu oluşturun. Klasördeki `_internal` dosyaları uygulamanın parçasıdır.
- **macOS:** `LiveDub.app` dosyasını *Uygulamalar* klasörüne sürükleyin. İlk açılışta mikrofon izni
  sorulur. İmzasız olduğu için ilk seferde sağ tık → *Aç* deyin.

Uygulamayı kendiniz derlemek için:
```bash
pip install -r requirements-build.txt
pyinstaller livedub.spec --noconfirm
```
Her platform kendi üzerinde derlenir: Windows'ta exe, Mac'te .app çıkar. Mac'iniz yoksa projeyi
GitHub'a yükleyip *Actions → build → Run workflow* çalıştırın
([.github/workflows/build.yml](.github/workflows/build.yml)). İki platformun zip'leri çalışmanın
*Artifacts* bölümünden indirilir. Mac sürümü Apple Silicon içindir. Başkalarına dağıtırken Apple
geliştirici hesabıyla imzalayıp notarize etmeniz gerekir.

### Geliştirici modu (kaynak koddan)

Kod üzerinde çalışırken derleme beklememek için `run_windows.bat` / `run_macos.command` betikleri
var. Bunlar Python 3.10+ sanal ortamını kurup uygulamayı kaynak koddan başlatır. Normal kullanım
için gerekli değildir.
```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip ...
.venv/bin/python -m livedub
```

Uygulamada **Ayarlar → API anahtarları** bölümüne anahtarınızı girin (ya da `GEMINI_API_KEY`, `OPENAI_API_KEY` vb.
ortam değişkenlerini tanımlayın). API gerektirmeyen **Ses testi** düğmesi çıkışta bip çalar ve giriş
seviyesini gösterir; cihaz kurulumunu doğrulamak için önce onu kullanın.

## Kullanım senaryoları

### 1) Windows'ta video / yayın dublajı

Kaynak = `Sistem sesi: tüm uygulamalar, dublaj hariç`, Çıkış = kulaklığınız ya da hoparlörünüz. Kurulum
gerekmez. LiveDub çalan her şeyi yakalar ama kendi dublajını hariç tutar (Windows 10 build 20348+ /
Windows 11).

**Orijinal ses** kaydırıcısı bu modda diğer uygulamaların sesini ayarlar:
- **%0:** yalnızca dublajı duyarsınız. Diğer uygulamalar %1'e kısılır, LiveDub yakaladığı sesi aynı oranda
  yükselttiği için çeviri etkilenmez. Windows, uygulama sesini kısılmış hâliyle verdiği için tamamen
  susturmak mümkün değil; susturulursa çevrilecek ses de kaybolur.
- **%100:** orijinal ile dublajı birlikte duyarsınız.
- Durdurunca sesler eski hâline döner. Uygulama çökse bile bir sonraki açılışta eski seviyeler geri
  yüklenir.

Dublaj yakalanan sese geri karışırsa model kendi sesini tekrar çevirir ve aynı cümleleri döngü hâlinde
tekrarlar. Bu yüzden `Sistem sesi: <cihaz>` kaynağı çıkışla aynı cihazsa uygulama otomatik olarak
"dublaj hariç" yakalamaya geçer.

**Alternatif, sanal kablo ile (uygulamaların sesine dokunmadan):**
1. Ücretsiz [VB-CABLE](https://vb-audio.com/Cable/) kurun.
2. *Ayarlar → Sistem → Ses → Uygulama ses düzeyi ve cihaz tercihleri* bölümünden tarayıcının/oynatıcının
   çıkışını **CABLE Input** yapın.
3. LiveDub'da Kaynak = `Sanal kablo: CABLE Output`, Çıkış = hoparlörünüz.
4. Orijinali arkada kısık duymak isterseniz **Orijinal ses** kaydırıcısını açın; dublaj konuşurken
   orijinal otomatik kısılır (ducking).

### 2) macOS'ta sistem sesi dublajı

macOS, uygulamaların sistem sesini doğrudan yakalamasına izin vermez; ücretsiz bir sanal kablo gerekir:
1. [BlackHole 2ch](https://existential.audio/blackhole/) kurun (`brew install blackhole-2ch`) ve Mac'i yeniden başlatın.
2. **Sistem Ayarları › Ses › Çıkış** bölümünde **BlackHole 2ch**'i seçin. Bu adım şart: kurmak tek başına
   yetmez, sistem sesi bu kabloya gitmezse LiveDub'a hiç ses gelmez.
3. LiveDub'da Kaynak = `Sanal kablo: BlackHole 2ch`. Çıkış = **MacBook hoparlörü / kulaklık**. Çıkışı
   "Varsayılan" bırakırsanız ve varsayılan BlackHole ise LiveDub dublajı otomatik olarak gerçek hoparlöre
   ya da kulaklığa yönlendirir. Multi-Output cihazı seçmeyin; dublaj tekrar BlackHole'a girer.
4. **Orijinal ses** kaydırıcısı: %0 = yalnızca dublaj, yükseltirseniz orijinali de duyarsınız.
5. İlk başlatmada macOS mikrofon izni ister. BlackHole da bir "mikrofon" sayılır, izin vermeniz gerekir.
   Reddettiyseniz: **Sistem Ayarları › Gizlilik ve Güvenlik › Mikrofon › LiveDub**'ı açıp uygulamayı yeniden
   başlatın.

İşiniz bitince Mac'in çıkışını tekrar hoparlöre ya da kulaklığa alın. Birkaç saniye hiç ses gelmezse
LiveDub nedenini arayüzde yazar (izin kapalı, kabloya ses gitmiyor vb.). Terminalde
`/Applications/LiveDub.app/Contents/MacOS/LiveDub --diagnose` izin durumunu ve cihazları gösterir.

### 3) Mikrofon → toplantıda çevrilmiş sesiniz (Zoom, Meet, Discord, Teams)

1. Kaynak = mikrofonunuz, hedef dil = karşı tarafın dili.
2. Çıkış = **CABLE Input** (Windows) veya **BlackHole 2ch** (macOS).
3. Toplantı uygulamasında mikrofon olarak **CABLE Output** / **BlackHole 2ch** seçin.

Türkçe konuşup İngilizce duyulmak için de *Gemini Live Translate* en hızlı ve en ucuz motordur.

### 4) Mikrofon → yanınızdaki kişiye hoparlörden

Kaynak = mikrofon, Çıkış = hoparlör. Mikrofon hoparlörü duyacağı için geri besleme koruması
açılır: siz konuşursunuz, dublaj çalar, sonra tekrar konuşursunuz (sıralı konuşma).

## Ayarlar hakkında

- **Gürültü azaltma:** mikrofonda `near_field`/`far_field`, sistem sesinde kapalı bırakın.
- **Sessizlik eşiği (Realtime GPT):** düşürmek (ör. 250 ms) gecikmeyi azaltır ama cümleleri bölebilir.
- **Konuşma hızı:** Türkçe çeviriler genelde kaynaktan uzundur; 1.1–1.2x dublajın geride kalmasını önler.
- **Kaskad + Deepgram:** otomatik dil algılama `nova-3` ile çalışır; belirli bir kaynak dil seçerseniz
  o dili destekleyen modeli seçin (Türkçe kaynak için `nova-2` en garantisidir).
- Model adları düzenlenebilir alanlardır; OpenAI yeni bir model yayınladığında kod değiştirmeden
  kullanabilirsiniz.

Ayarlar ve anahtarlar `~/.livedub/settings.json` dosyasında **düz metin** saklanır.

## Terminal modu

```bash
python -m livedub --list-devices
python -m livedub --test-audio --source "lb:{...}" --output "out:Hoparlör (Realtek(R) Audio)"
python -m livedub --headless --engine realtime --target tr --source "in:CABLE Output (VB-Audio Virtual Cable)"
```

## Testler

```bash
python tests/mock_engines.py
```
Üç motoru da yerel sahte sunuculara karşı çalıştırır (API anahtarı ve ses cihazı gerekmez).

## Proje yapısı

```
livedub/
  audio/devices.py     cihaz listeleme, geri besleme riski tespiti
  audio/capture.py     mikrofon/sanal kablo (sounddevice) + Windows WASAPI loopback (soundcard)
  audio/player.py      düşük gecikmeli çıkış, orijinal sesi karıştırma ve kısma
  engines/             openai_translate.py, openai_realtime.py, cascade.py
  session.py           yakala → motor → çal hattı (arka plan iş parçacığı + asyncio)
  gui.py               PySide6 arayüz
  cli.py               terminal modu
```

## Sorun giderme

- **Hiç ses gelmiyor:** *Ses testi* ile giriş çubuğunun hareket ettiğini doğrulayın. Windows'ta
  loopback, uygulamalar *özel mod* (exclusive) kullanırken çalışmaz.
- **Dublaj aynı cümleleri döngü hâlinde tekrarlıyor:** dublaj, yakalanan sese geri karışıyor. Windows'ta
  kaynak olarak `Sistem sesi: tüm uygulamalar, dublaj hariç` seçin. macOS'ta çıkışın BlackHole'a
  gitmediğinden emin olun.
- **OpenAI Realtime Translate Türkçe konuşmuyor:** model Türkçe çıkış desteklemez; *Gemini Live Translate* seçin.
- **Gemini "kota" hatası:** ücretsiz katmanın kullanım sınırı dolmuştur. Bir süre bekleyin ya da Google AI
  Studio'da faturalandırmayı açın.
- **"API anahtarı geçersiz":** anahtarı ve hesabınızda Realtime API erişimi olduğunu kontrol edin.
