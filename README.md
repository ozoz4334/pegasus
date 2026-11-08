# ✈️ Pegasus Kampanya Takip Botu

Pegasus Hava Yolları'nın **herkese açık** kampanya sayfalarını GitHub Actions üzerinde **5 dakikada bir** kontrol eder.
Yeni bir kampanya yayımlandığında, mevcut bir kampanyanın önemli bir alanı değiştiğinde veya bir kampanya
gerçekten sona erdiğinde Telegram'dan Türkçe bildirim gönderir. **Değişiklik yoksa Telegram'a hiçbir mesaj gitmez.**

* Sunucu, ücretli servis veya sürekli açık bilgisayar gerekmez.
* Botun hafızası depodaki `.state/campaigns.json` dosyasıdır; her çalışmada GitHub'a geri yazılır.
* Python 3.12, `requests`, `BeautifulSoup4`. Tarayıcı otomasyonu (Selenium/Playwright) **kullanılmaz**; sayfalar sunucu tarafında üretildiği için gerek yoktur.

---

## İçindekiler

1. [Nasıl çalışır?](#1-nasıl-çalışır)
2. [Takip edilen kaynaklar ve sınırlar](#2-takip-edilen-kaynaklar-ve-sınırlar)
3. [Kurulum (sıfırdan, adım adım)](#3-kurulum-sıfırdan-adım-adım)
4. [Telegram kurulumu](#4-telegram-kurulumu)
5. [Çalıştırma ve doğrulama](#5-çalıştırma-ve-doğrulama)
6. [5 dakikalık zamanlama hakkında dürüst not](#6-5-dakikalık-zamanlama-hakkında-dürüst-not)
7. [GitHub Actions kotası: public ve private farkı](#7-github-actions-kotası-public-ve-private-farkı)
8. [Sorun giderme](#8-sorun-giderme)
9. [Botu durdurma ve yeniden başlatma](#9-botu-durdurma-ve-yeniden-başlatma)
10. [Tasarım ayrıntıları](#10-tasarım-ayrıntıları)
11. [Yerelde çalıştırma ve testler](#11-yerelde-çalıştırma-ve-testler)

---

## 1. Nasıl çalışır?

```
GitHub Actions (her ~5 dk)
   │
   ├─ 1. .state/campaigns.json dosyasını yükle (bozuksa yedekle, sessiz yeniden kur)
   ├─ 2. Önceki çalışmalardan kalan "bekleyen" bildirimleri yeniden dene
   ├─ 3. 4 liste sayfasını çek (en fazla 2 eşzamanlı) → kampanya kartlarını çıkar, slug ile tekilleştir
   ├─ 4. Yeni / değişmiş / sıradaki kampanyaların detay sayfasını çek (her çalışmada tümünü değil)
   ├─ 5. Önemli alanlardan hash üret, eski kayıtla karşılaştır → olaylar: YENİ / GÜNCELLENDİ / SONA ERDİ
   ├─ 6. Olayları "pending" olarak kaydet → Telegram'a gönder → başarılıysa "sent" yap
   └─ 7. State yalnızca anlamlı değişiklik varsa (veya günde 1 kez) commit + push edilir
```

**Hiçbir şey değişmediyse:** Telegram'a mesaj yok, depoya commit yok.

### Bildirim türleri

| Tür | Ne zaman? |
|---|---|
| 🟢 **Yeni kampanya** | Daha önce kayıtlı olmayan, hâlâ geçerli bir kampanya yayımlandığında |
| 🟡 **Güncellendi** | İndirim, fiyat, tarihler, hatlar, promosyon kodu, koşullar, BolPuan, avantaj vb. önemli bir alan değiştiğinde. Mesajda **yalnızca değişen alanlar** (`eski → yeni`) gösterilir |
| 🔴 **Sona erdi** | (a) Bitiş tarihi geçtiyse, (b) Pegasus kampanyayı "Geçmiş Kampanyalar" bölümüne taşıdıysa veya (c) üst üste 3 sağlıklı taramada listeden kaybolup sayfası da kaldırıldıysa (404/410). Aynı kampanya için **bir kez** |

Tek bir hatalı HTTP isteği, açılmayan sayfa, değişen HTML yapısı veya internet kesintisi **asla** "kampanya bitti" bildirimi üretmez.

### Gürültü sayılan ve bildirim üretmeyen değişiklikler

HTML boşlukları ve noktalama, sayfa tasarımı, görsel dosyaları, kartların sırası, izleme parametreleri (`?utm_…`),
sayaçlar/gün-saat göstergeleri, sayfanın yeniden yüklenmesi, aynı kampanyanın başka kaynakta da görünmesi ve
uzun metinlerdeki küçük yazım farkları (yüzde 98,5'ten fazla aynı kalan, rakam içermeyen değişiklikler).
Rakam/tarih değişimi ise **her zaman** önemlidir.

---

## 2. Takip edilen kaynaklar ve sınırlar

2026-11-08 tarihinde canlı olarak doğrulandı (hepsi giriş gerektirmez, HTTP 200, sunucu tarafında üretilmiş HTML):

| Kaynak | İçerik |
|---|---|
| `https://www.flypgs.com/kampanyali-ucak-biletleri` | Güncel kampanyalar, iş birlikleri, geçmiş kampanyalar |
| `https://www.flypgs.com/bolbol/kampanyalar` | BolBol / Genç BolBol / iş ortaklığı kampanyaları |
| `https://www.flypgs.com/sana-ozel-kampanyalar` | Herkese açık kartlar (kişiye özel bölüm giriş gerektirir ve **takip edilmez**) |
| `https://www.flypgs.com/kampanyali-ucak-biletleri/gecmis-kampanyalar` | Bitmiş kampanyalar (sona erme kanıtı) |
| `…/kampanyali-ucak-biletleri/<slug>` | Her kampanyanın detay sayfası (tarihler, koşullar, hat listesi) |

`robots.txt` bu yolları yasaklamaz (`Allow: /`). Bot yalnızca bu sayfaları okur; arama (`/arama/`, `*search*`) gibi yasaklı yollara gitmez.

**Bilerek yapılmayanlar / sınırlar**

* Üyelik girişi gerektiren, kişiye özel kampanyalar takip edilemez; takip edildiği iddia edilmez.
* Yalnızca mobil uygulamada duyurulup web'de sayfası olmayan kampanyalar görülemez.
* CAPTCHA veya erişim kısıtlaması aşılmaya çalışılmaz. HTTP 403/429 alınırsa kaynak o çalışmada atlanır, `Retry-After` süresine uyulur.
* Kategori (yurt içi/yurt dışı, banka, iş ortaklığı, BolPuan, bagaj…) başlık ve metin anahtar kelimelerinden **tahmin edilir**; resmi bir kategori alanı yoktur.
* Sunucu `ETag`/`Last-Modified` göndermiyor ve `Cache-Control: no-store` kullanıyor (ölçüldü). Bu yüzden "koşullu GET" ile indirme tasarrufu **yapılamaz**; tasarruf bunun yerine detay sayfalarını kovalara bölerek sağlanır (her kampanyanın detayı ~30 dakikada bir, yeni/değişen kartlar anında).

Tipik bir çalışmada ~4 liste isteği + 1-3 detay isteği yapılır (≈ 5-7 HTTP isteği).

---

## 3. Kurulum (sıfırdan, adım adım)

### 3.1 GitHub hesabı ve depo (repository)

1. <https://github.com> adresinde hesap açın (yoksa) ve giriş yapın.
2. Sağ üstteki **+** → **New repository**.
3. **Repository name:** `pegasus-campaign-bot` (istediğiniz adı verebilirsiniz).
4. **Public** veya **Private** seçin → bkz. [Bölüm 7](#7-github-actions-kotası-public-ve-private-farkı). *Kısaca: 5 dakikalık takip için **Public** önerilir.*
5. "Add a README" vb. kutuları **işaretlemeyin**. **Create repository**'e basın.

### 3.2 Proje dosyalarını yükleme

**Yöntem A – Tarayıcıdan (en kolay)**

1. Yeni boş depo sayfasında **uploading an existing file** bağlantısına tıklayın.
2. Bilgisayarınızdaki `pegasus-campaign-bot` klasörünün **içindeki her şeyi** (klasörün kendisini değil) sürükleyip bırakın.
   Şunların yüklendiğinden emin olun: `main.py`, `scraper.py`, `campaign_tracker.py`, `telegram_notifier.py`, `state_manager.py`, `config.py`, `textutil.py`, `requirements.txt`, `requirements-dev.txt`, `README.md`, `.gitignore`, `tests/`, **`.state/`** ve **`.github/`**.
3. `.github` ve `.state` gibi noktayla başlayan klasörler Windows/macOS'ta gizli olabilir. Görünmüyorsa:
   Windows Gezgini → **Görünüm → Göster → Gizli öğeler**; macOS Finder → `Cmd + Shift + .`
4. Sayfanın altında **Commit changes** düğmesine basın.
5. Depoda `.github/workflows/pegasus_tracker.yml` dosyasının **tam bu yolda** durduğunu kontrol edin. (Tarayıcı yüklemesi noktalı klasörleri atlarsa Yöntem B'yi kullanın.)

**Yöntem B – Git ile**

```bash
cd pegasus-campaign-bot
git init -b main
git add .
git commit -m "İlk sürüm"
git remote add origin https://github.com/KULLANICI_ADINIZ/pegasus-campaign-bot.git
git push -u origin main
```

### 3.3 GitHub Actions'ı etkinleştirme

1. Depoda **Actions** sekmesine girin.
2. "Workflows aren't being run on this repository" veya yeşil bir düğme görürseniz **I understand my workflows, go ahead and enable them** seçeneğine basın.
3. Soldaki listede **Pegasus Kampanya Takibi** görünmelidir.

### 3.4 Gerekli repository izinleri (önemli)

Bot, hafızasını depoya yazabilmek için yazma iznine ihtiyaç duyar:

1. **Settings → Actions → General**.
2. En altta **Workflow permissions** bölümünde **Read and write permissions** seçin → **Save**.
3. Ana dalınız (genellikle `main`) için "branch protection" kuralı (push'u engelleyen) tanımlamayın.

### 3.5 Secret'ları ekleme

Önce [Bölüm 4](#4-telegram-kurulumu) ile token ve chat id'yi alın, sonra:

1. **Settings → Secrets and variables → Actions → New repository secret**.
2. **Name:** `TELEGRAM_BOT_TOKEN` — **Secret:** BotFather'ın verdiği token → **Add secret**.
3. Tekrar **New repository secret**: **Name:** `TELEGRAM_CHAT_ID` — **Secret:** chat id'niz → **Add secret**.

> Token ve chat id kod içine **yazılmaz**; yalnızca burada saklanır. Loglarda otomatik olarak `***` ile maskelenir.

---

## 4. Telegram kurulumu

1. **BotFather'ı açın:** Telegram'da arama kutusuna `@BotFather` yazın ve mavi tikli resmi hesabı açın (<https://t.me/BotFather>).
2. **Bot oluşturun:** `/newbot` yazıp gönderin. Önce botun görünen adını (ör. `Pegasus Kampanya`), sonra `bot` ile biten benzersiz kullanıcı adını (ör. `pegasus_kampanya_takip_bot`) yazın.
3. **Token'ı alın:** BotFather "Use this token to access the HTTP API:" diyerek `123456789:AA…` biçiminde bir değer verir. Bu sizin **bot token**'ınızdır. Kimseyle paylaşmayın; paylaşırsanız BotFather'da `/revoke` ile yenileyin.
4. **Botunuza ilk mesajı gönderin:** BotFather'ın verdiği `t.me/…` bağlantısına tıklayın → **Start** → "merhaba" yazın. (Bot, kendisine hiç yazmamış kişiye mesaj atamaz.)
5. **Chat ID'yi bulun:** Tarayıcıda aşağıdaki adresi açın (`TOKEN` yerine kendi token'ınızı yazın):

   ```
   https://api.telegram.org/botTOKEN/getUpdates
   ```

   Çıkan metinde `"chat":{"id":123456789,…` kısmındaki sayı chat id'nizdir. Sonuç boşsa (`"result":[]`) botunuza yeniden bir mesaj yazıp sayfayı yenileyin.
   *Grup kullanacaksanız:* botu gruba ekleyip grupta bir mesaj yazın; grup id'leri `-100…` gibi **negatif** sayılardır.
6. **Secret'ları ekleyin:** [Bölüm 3.5](#35-secretları-ekleme).
7. **Bağlantıyı test edin:** Depoda **Actions → Pegasus Kampanya Takibi → Run workflow**, **"Telegram test mesajı gönder"** kutusunu işaretleyin → **Run workflow**. Telegram'a şu mesaj gelmelidir:

   ```
   ✅ Pegasus Kampanya Takip Botu Aktif
   ⏱️ Planlanan kontrol aralığı: 5 dakika
   📡 Takip sistemi çalışıyor.
   🔔 Yalnızca kampanya değişikliklerinde bildirim gönderilecek.
   ```

   Bu mesaj **yalnızca** siz istediğinizde gönderilir; normal çalışmada durum mesajı atılmaz.

---

## 5. Çalıştırma ve doğrulama

### 5.1 Workflow'u elle çalıştırma

**Actions → Pegasus Kampanya Takibi → Run workflow → Run workflow**.
İlk çalışma "ilk kurulum"dur: mevcut tüm kampanyaları **sessizce** kaydeder (Telegram'a geçmiş kampanyalar gönderilmez) ve `.state/campaigns.json` dosyasını commit eder.

İsteğe bağlı: **"Kuru çalıştırma"** kutusu mesaj göndermeden ve hafızayı değiştirmeden sonucu loga yazar.

### 5.2 5 dakikalık zamanlamayı doğrulama

1. İlk elle çalışmadan sonra 10-20 dakika bekleyin.
2. **Actions** sekmesinde **Pegasus Kampanya Takibi** altında, **Event: schedule** olan çalışmaların art arda (yaklaşık 5 dakika arayla) listelendiğini görün. Bu çalışmalar ilk kurulumdan sonra çoğunlukla "state değişmedi" diyerek biter.
3. Zamanlama garantili değildir (bkz. [Bölüm 6](#6-5-dakikalık-zamanlama-hakkında-dürüst-not)).

### 5.3 Hafızanın korunduğunu test etme

1. Depoda `.state/campaigns.json` dosyasını açın; kampanyalarınızın kayıtlı olduğunu görün (`"initialized": true`).
2. Workflow'u elle iki kez daha çalıştırın. **Yeni commit oluşmamalı** (değişiklik yok) ve Telegram'a **mesaj gelmemeli**.
3. Çalışma logunda `Bitti: gönderilen=0 bekleyen=0 state_yazıldı=False` satırını arayın.
4. Hafıza günde bir kez (heartbeat) ve gerçek değişiklikte güncellenir; commit geçmişinde `chore(state): kampanya durumu güncellendi` kayıtlarını görebilirsiniz.

*(İleri düzey, isteğe bağlı) Bildirimi uçtan uca denemek:* `.state/campaigns.json` içinde etkin bir kampanyanın `"discount"` değerini elle başka bir şeye çevirin ve aynı kampanyada `"list_fp": ""` yapıp commit edin; sonraki çalışmada Telegram'a "GÜNCELLENDİ" mesajı gelir (eski → yeni gösterilir).

### 5.4 GitHub Actions loglarını görüntüleme

**Actions → bir çalışmaya tıklayın → `track` işi → "Kampanyaları kontrol et" adımı**. Loglar İstanbul saatiyle yazılır. Önemli satırlar:

```
Kaynak kampanyali: 28 kart (16 güncel)
Tarama: 29 kampanya listede, 2 detay isteği, toplam 6 HTTP isteği
YENİ kampanya: …            GÜNCELLENDİ: …            SONA ERDİ (date): …
Bitti: gönderilen=1 bekleyen=0 state_yazıldı=True
```

Kritik sorunlar çalışma özetinde **sarı uyarı / kırmızı hata** olarak da görünür. Token ve chat id logda `***` olarak maskelenir.

### 5.5 Kullanım dakikalarını görüntüleme

GitHub'da sağ üst profil resmi → **Settings → Billing and plans → Usage** (bazı hesaplarda *Plans and usage*), ürün olarak **Actions** seçin. Kuruluşlarda: *Organization → Settings → Billing*. Aylık harcamayı ve kalan ücretsiz dakikaları buradan izleyin.

---

## 6. 5 dakikalık zamanlama hakkında dürüst not

`cron: '*/5 * * * *'`, GitHub Actions'ın desteklediği **en kısa** aralıktır. Ancak:

* GitHub zamanlanmış çalışmaları **tam zamanında başlatmayı garanti etmez**. Yoğun saatlerde (özellikle saat başları) çalışmalar **dakikalarca gecikebilir**, bazı çalışmalar **hiç başlamayabilir (atlanabilir)**.
* Pratikte aralık çoğu zaman 5-15 dakika arasında dalgalanır. Günde teorik olarak 288 kontrol hedeflenir; gerçekleşen sayı daha düşük olabilir.
* Bot bu duruma dayanıklıdır: geciken veya atlanan çalışmadan sonra kaldığı yerden devam eder; kaçırılan değişiklikler bir sonraki çalışmada yakalanır. `concurrency` ayarı iki çalışmanın aynı anda state'e yazmasını engeller.
* Public depolarda **60 gün boyunca depo etkinliği olmazsa** GitHub zamanlanmış workflow'ları kapatabilir. Botun günlük "heartbeat" commit'i bunu önlemek içindir; yine de GitHub'dan "scheduled workflows disabled" uyarısı alırsanız **Actions → workflow → Enable workflow** ile yeniden açın.
* Gece/gündüz, hafta sonu dahil kesintisiz çalışır; saat dilimi `Europe/Istanbul` olarak ayarlıdır.

---

## 7. GitHub Actions kotası: public ve private farkı

| | Public depo | Private depo (GitHub Free) |
|---|---|---|
| Standart runner dakikaları | **Ücretsiz, sınırsız** | **Ayda 2.000 dakika** |
| Hafıza dosyası ve loglar | Herkese açık görünür (yalnızca zaten herkese açık kampanya bilgileri; token/chat id **görünmez**) | Yalnızca siz görürsünüz |
| 5 dakikalık takip | Sorunsuz | **Ücretsiz kotayı aşar** (aşağıya bakın) |

**Aylık dakika hesabı**

* 5 dakikada bir: 12 çalışma/saat × 24 × 30 = **8.640 çalışma/ay** (günde 288).
* Private depolarda her iş **tam dakikaya yukarı yuvarlanır**. Bu botun bir çalışması tipik olarak ~25-50 saniye sürer (kurulum + tarama) → **1 dakika** faturalanır.
* Tahmini aylık tüketim = `8.640 × ceil(ortalama süre saniye / 60)`

| Ortalama çalışma süresi | Faturalanan | Aylık dakika |
|---|---|---|
| ≤ 60 sn | 1 dk | **≈ 8.640** |
| 61-120 sn | 2 dk | ≈ 17.280 |

Kendi gerçek ortalamanızı **Actions** sekmesindeki çalışma sürelerinden ve [Bölüm 5.5](#55-kullanım-dakikalarını-görüntüleme)'daki kullanım sayfasından kontrol edin.

**Sonuç:** Private + ücretsiz plan ile 5 dakikalık takip **mümkün değildir** (8.640 > 2.000). Seçenekleriniz:

1. **Public depo kullanın** (önerilir; sınırsız). Not: hafıza dosyasında yalnızca resmi sitedeki herkese açık kampanya bilgileri bulunur.
2. Private kalmak istiyorsanız `.github/workflows/pegasus_tracker.yml` içinde `cron: '*/5 * * * *'` satırını örneğin `cron: '*/30 * * * *'` yapın (≈ 1.440 dk/ay).
3. Ücretli planlarda dahil dakikaları (Pro/Team: 3.000) ve aşım fiyatını kontrol edin.

Pegasus'u yormamak için bot çalışma başına yalnızca birkaç istek yapar; 429 / `Retry-After` ve geçici kısıtlamalara uyar.

---

## 8. Sorun giderme

| Belirti | Olası neden / çözüm |
|---|---|
| Hiç çalışma görünmüyor | Actions etkin mi? (3.3) İlk elle çalıştırmayı yaptınız mı? Public depoda 60 gün hareketsizlik sonrası kapanmış olabilir → *Enable workflow* |
| Çalışma kırmızı: `Telegram bilgileri (secrets) tanımlı değil` | `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` adları **harfi harfine** doğru olmalı (3.5). Bildirimler kaybolmaz; Secret eklenince gönderilir |
| `Telegram yetki hatası` (401/403/404) | Token yanlış/iptal edilmiş ya da bot sohbetten çıkarılmış / engellenmiş. Botunuza yeniden `Start` deyin, token'ı kontrol edin |
| Telegram test mesajı gelmiyor | Botunuza hiç mesaj yazmadınız (4. adım); chat id yanlış; token'da boşluk var |
| `State push edilemedi` | 3.4'teki **Read and write permissions** ayarı eksik veya ana dalda branch protection var |
| `Kaynak alınamadı … HTTP 403/429/503` | Pegasus geçici kısıtlama/bakım uyguluyor. Bot o kaynağı atlar, durumu bozmaz, sonraki çalışmada tekrar dener. Süreklilik varsa bir süre bekleyin |
| `kampanya kartı bulunamadı (HTML yapısı değişmiş olabilir)` | Pegasus sayfa tasarımını değiştirmiş. Bildirim **gönderilmez**, hafıza korunur. `scraper.py` içindeki sınıf adlarının güncellenmesi gerekir |
| `State dosyası bozuk` | Dosya `.state/campaigns.json.corrupt-…` olarak yedeklenir ve bot sessizce yeniden kurulur (geçmiş kampanyalar için mesaj gitmez) |
| Çok fazla commit | Bu normal değil. Yalnızca gerçek değişiklikte ve günde bir kez commit atılmalı. Sürekli commit varsa `.state` altındaki dosyayı elle düzenlediyseniz biçimini kontrol edin |
| Aynı bildirim iki kez geldi | Telegram'a gönderim başarılı olduktan hemen sonra çalışma çökerse tam bir-kez garantisi **mümkün değildir**; bu nadir durumda en fazla 1 tekrar olabilir (bkz. Bölüm 10) |

---

## 9. Botu durdurma ve yeniden başlatma

* **Durdurma:** Actions → **Pegasus Kampanya Takibi** → sağ üstte **⋯ → Disable workflow**. Hafıza dosyası korunur.
* **Yeniden başlatma:** Aynı menüden **Enable workflow**, ardından isterseniz **Run workflow**. Kapalı kaldığınız sürede olan değişiklikler, ilk çalışmada normal "yeni/güncellendi/sona erdi" bildirimleri olarak (bir kerelik toplu) gelebilir.
* **Tamamen sıfırlama:** `.state/campaigns.json` dosyasını README'deki boş şablonla (başlangıçta gelen dosya) değiştirin; bir sonraki çalışma sessizce yeniden kurulum yapar.
* **Kalıcı silme:** Workflow dosyasını silin veya depoyu kaldırın.

---

## 10. Tasarım ayrıntıları

### Kalıcı hafıza (`.state/campaigns.json`)

Her kampanya için: kimlik (kalıcı URL slug'ı; varsa Pegasus'un `promotion-id`'si ayrıca), başlık, resmi URL, kategori, içerik/koşullar, indirim, fiyat, satış/uçuş dönemleri, başlangıç/bitiş tarihleri, promosyon kodları, BolPuan, hat listesi, son görülme, son doğrulama, **içerik hash'i**, **son bildirilen hash**, aktif/sona ermiş durumu ve bildirim durumu (`baseline / pending / sent / failed`).

* Yazma **atomiktir** (geçici dosya → `os.replace`); yarım kalan yazma dosyayı bozmaz.
* `last_seen` gibi her çalışmada değişen alanlar tek başına commit tetiklemez; günde bir kez "heartbeat" ile yazılır.
* Aynı anda tek çalışma (`concurrency`); push çakışmasında 5 kereye kadar `pull --rebase` ile yeniden denenir (bizim state'imiz korunur).

### Bildirim güvenilirliği (pending → sent)

1. Olay bulunur → **outbox'a `pending`** olarak yazılır ve state diske kaydedilir.
2. Telegram'a gönderilir (429 `retry_after`'a uyma, artan beklemeli yeniden deneme, HTML hatasında düz metne düşme).
3. Yalnızca **başarılı** gönderimde `sent` yapılır ve `last_notified_hash` güncellenir. Başarısızsa `pending` kalır; **sonraki çalışma yeniden dener**. Kalıcı reddedilen (ör. 400) mesaj `failed` olur.
4. Python adımı hata verse bile workflow, o ana kadar kaydedilen state'i commit eder.

**Tam bir-kez teslim mümkün değildir:** Telegram mesajı aldıktan hemen sonra GitHub çalışması çökerse "gönderildi" bilgisi kaydedilemez ve mesaj bir kez daha gidebilir. Tasarım bunu iki yolla en aza indirir: her başarılı gönderimden hemen sonra state diske yazılır ve olay anahtarı (`kampanya + tür + içerik hash'i`) bekleyen kayıtlarda tekrarı engeller. Tersi yönde (mesaj hiç gönderilmeden çökme) bildirim asla kaybolmaz.

### Sona erme kararı

Bitiş tarihi (İstanbul saati, gün sonu + 2 saat tolerans) geçmişse, kart Pegasus'un "Geçmiş Kampanyalar" bölümüne taşınmışsa ya da kampanya **üst üste 3 sağlıklı taramada** (tüm zorunlu kaynaklar yanıt verirken) listeden kaybolmuş **ve** detay sayfası 404/410 dönüyorsa. Bir kaynak hata veriyorsa o taramada kayıp sayılmaz. Sayfası hâlâ yayında olan ama listelenmeyen kampanya "bitti" sayılmaz.

### Dosyalar

| Dosya | Görevi |
|---|---|
| `main.py` | Komut satırı (`--dry-run`, `--send-test`), çalışma akışı, outbox teslimi |
| `config.py` | Kaynaklar, eşikler, süreler, saat dilimi |
| `scraper.py` | HTTP istemcisi (yeniden deneme, 429, bağlantı yeniden kullanımı), liste/detay ayrıştırma |
| `campaign_tracker.py` | Hash, alan bazlı fark, YENİ/GÜNCELLENDİ/SONA ERDİ durum makinesi |
| `state_manager.py` | Atomik yazma, bozuk dosya kurtarma, heartbeat, outbox |
| `telegram_notifier.py` | Mesaj biçimleri, bölme (4096), güvenli HTML, gönderim ve yeniden deneme |
| `textutil.py` | Türkçe tarih/metin normalizasyonu |
| `tests/` | `pytest` testleri (gerçek siteye/Telegram'a bağlanmaz) |
| `.github/workflows/pegasus_tracker.yml` | 5 dakikalık zamanlama, commit/push |

---

## 11. Yerelde çalıştırma ve testler

Python 3.12 gerekir.

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate        macOS/Linux:  source .venv/bin/activate
pip install -r requirements-dev.txt

python -m pytest -q                         # ağsız testler
python main.py --dry-run --state-path tmp_state.json   # canlı siteyi okur, Telegram'a göndermez, state'e yazmaz
```

`--dry-run` tespit edilen değişiklikleri terminalde gösterir. İlk `--dry-run` "ilk kurulum" olarak 0 bildirim üretir; değişiklikleri görmek için önce `--dry-run` olmadan (Telegram bilgisi vermeden) bir kez çalıştırıp state oluşturabilirsiniz:

```bash
python main.py --state-path tmp_state.json     # sessiz ilk kurulum (bildirim yok → Telegram bilgisi gerekmez)
python main.py --dry-run --state-path tmp_state.json
```

Telegram'ı yerelde denemek için `TELEGRAM_BOT_TOKEN` ve `TELEGRAM_CHAT_ID` ortam değişkenlerini tanımlayıp `python main.py --send-test` çalıştırın.

Testler şu senaryoları kapsar: ilk çalıştırma, değişiklik olmaması, yeni kampanya, indirim/fiyat/bitiş tarihi değişimi, sona erme (tarih, geçmişe taşınma, sayfanın kaldırılması), iki kaynakta aynı kampanya, Telegram hatası, yeniden başlatma, HTML yapısı değişimi, geçici internet kesintisi, bozuk JSON, tekrar bildirim yok, kart sırası değişimi, başarısız taramanın sahte bitiş üretmemesi, bildirimin kaydı, bekleyen bildirimin yeniden denenmesi.
