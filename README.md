# Binance Spot deneme botu

Türkçe panelden yönetilen, tek USDT işlem çiftinde otomatik **deneme alım/satımı** yapan uygulama. İşlem ve toplam net kâr/zarar, komisyonlar, sabit zarar durdurma ve iki farklı kâr takip modu içerir. Gerçek emir göndermez; API anahtarı veya borsa hesabı gerektirmez. Stratejilerin gelecekteki kârı garanti edilmez.

## Windows için ZIP indir

**[Hazır ZIP paketini indir](https://github.com/dereli2002/AL-SAT/raw/refs/heads/main/BOTU-INDIR.zip)**

Özel depoda GitHub hesabınızla oturum açmanız gerekir. Bağlantı dosya sayfasına götürürse **Download raw file** indirme okunu kullanın.

ZIP'i klasöre çıkarıp **BASLAT.bat** dosyasına çift tıklayın. Python 3.12 önerilir. İlk çalıştırmada gerekli saat dilimi paketi kurulur; açılan konsol penceresi açık kalmalıdır. Gerçek emir gönderilmez.

## Çalıştırma

Python **3.12** önerilir (en az 3.10). Uygulama standart kütüphane ve SQLite kullanır; saat dilimleri için `requirements.txt` içindeki sabitlenmiş `tzdata` paketi kurulur. Node gerekmez.

**Windows:** ZIP paketini bir klasöre çıkarın; Python 3.12 kuruluysa **BASLAT.bat** dosyasına çift tıklayın. İlk açılışta yerel sanal ortam ve saat dilimi paketi hazırlanır, panel varsayılan tarayıcıda açılır. Konsol penceresi açık kalmalıdır. Windows başlatıcısı Linux bulut makinesinde çalıştırılmamıştır.

**Linux/macOS:**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python baslat.py
```

Bulut makinesinde tarayıcı açmadan başlatmak için:

```bash
cd /workspace/crypto-spot-bot
/workspace/.venvs/crypto-spot-bot/bin/python baslat.py --no-browser --port 8080
```

Panel sunucunun `/` yolundadır. Bu bulut kurulum ekranı localhost web önizlemesi sunmaz; paneli görüntülemek için sunucuya tarayıcı erişimi bulunan bir çalışma ortamı gerekir. HTTP durumunu aynı makineden kontrol edebilirsiniz:

```bash
curl --fail http://127.0.0.1:8080/api/health
python3 -m unittest discover -s tests -v
```

İlk açılışta yeni işlemler duraklatılmıştır. Varsayılan veri kaynağı Binance'in herkese açık `data-api.binance.vision` servisidir. Güncel veri geldikten sonra panelden başlatın. Ağ erişimi engelliyse durum panelinde hata görünür; uygulama kendiliğinden demo verisine geçmez. İsterseniz ayarlardan **sentetik demo** kaynağını açıkça seçebilirsiniz. Demo sonuçları borsa verisi değildir.

## Kullanım ve varsayılanlar

EMA 12/26 kesişimi, RSI 14 dönüşü ve 20 mum kırılımı stratejileri karşılaştırılabilir. Sinyaller kapanmış mumları kullanır; uygulama açık pozisyon varken ikinci alış yapmaz. Kaldıraç, açığa satış ve çoklu pozisyon bulunmaz.

| Ayar | Varsayılan |
| --- | --- |
| İşlem çifti / mum / strateji | BTCUSDT / 5 dakika / EMA 12/26 |
| Deneme başlangıç bakiyesi | 10.000 USDT |
| Alış bütçesi, alış ücreti dahil | 1.000 USDT; kullanılabilir nakitle sınırlı |
| Alış / satış komisyonu | Her yönde %0,10 |
| Fiyat kayması | Her yönde %0,05 |
| Sabit zarar durdur | Gerçekleşen alış fiyatının %2 altı |
| Seçili takip modu | Zirve fiyattan %1 geri çekilme |
| Takip aktivasyonu | Pozisyon maliyetine göre %1 net kâr |
| Net kâr modu geri verme oranı | Ulaşılan net kârın %25'i |
| Günlük zarar sınırı | Gün başlangıcı özkaynağının %5'i |
| Günlük saat dilimi | Europe/Istanbul |

Asıl varsayılanlar `bot/core.py` içindeki `DEFAULT_CONFIG` ve `bot/service.py` içindeki `APP_DEFAULTS` değerleridir. Ayarları değiştirmek için botu duraklatın ve açık deneme pozisyonunu kapatın. İşlem geçmişi oluştuktan sonra çift/veri kaynağı veya başlangıç bakiyesi değişikliği için deneme defterini sıfırlayın. Sıfırlama önceki deneme bakiyesini ve görünür işlem geçmişini temizler.

**Komisyonlar tahmindir.** Alanlarda `0.10`, **%0,10** anlamına gelir. Binance hesabının güncel komisyonu otomatik sorgulanmaz; hesap seviyesi, kampanya ve BNB indirimleri uygulanmaz. Ücretlerin USDT ödendiği varsayılır. Kayma alış fiyatını yükseltir, satış fiyatını düşürür. Binance miktar adımı ve minimum emir tutarı alış miktarına uygulanır; emir defteri derinliği ve kısmi gerçekleşmeler modellenmez.

Gerçekleşmiş kâr/zarar, net satış geliri eksi alış maliyetidir; alış ve satış ücretleri dahildir. Açık kâr/zarar, mevcut pozisyonun satış ücreti ve kayma sonrası tahmini tasfiye değerinden hesaplanır. Toplam, gerçekleşmiş ve açık kâr/zararın toplamıdır. İşlem getiri yüzdesi alış maliyetine, toplam getiri yüzdesi başlangıç bakiyesine göredir.

## Stoplar ve kalıcı kayıt

Sabit stop, seçili takip moduyla birlikte çalışır. `stop_loss_pct=0` sabit stopu, `daily_loss_limit_pct=0` günlük limiti, `trailing_mode=off` kâr takibini kapatır.

- **Zirve fiyattan geri çekilme:** Zirve 110 USDT, mesafe %2 ise satış eşiği 107,80 USDT'dir. Zirve yükseldikçe eşik yükselir; aşağı gevşemez. Bu modda eşik komisyon sonrası pozitif kârı tek başına garanti etmez.
- **Net kârdan geri verme:** Komisyon ve kayma sonrası görülen en yüksek kâr 10 USDT, geri verme %25 ise net kâr 7,50 USDT'ye indiğinde çıkış tetiklenir. Bu, fiyatın %25 düşmesi anlamına gelmez.

Takip modu, net kâr aktivasyon eşiğine ulaşıldıktan sonra devreye girer. Koruyucu çıkışlar strateji sinyalinden önce değerlendirilir; aynı fiyat gözleminde stop sonrası yeniden alış yapılmaz. Stoplar gözlenen fiyat üzerinden simüle edilir; fiyat boşluğunda stop eşiğinden gerçekleşme garantisi yoktur.

Günlük limit açık zarar ve ücretleri de kapsar; aşılınca pozisyon kapanır ve o gün yeni alışlar kilitlenir. Yeni gün İstanbul saatine göre başlar. Taşınan pozisyonda günlük özkaynak başlangıcı, yeni günün ilk başarılı fiyat gözlemidir.

Veriler varsayılan olarak **`.data/paper.sqlite3`** içinde saklanır. Yeniden başlatmada bakiye, işlemler, açık pozisyon, takip zirveleri ve günlük kilit geri yüklenir; yeni strateji işlemleri duraklatılmış başlar. **Duraklatma sırasında fiyat takibi ve açık pozisyon stopları çalışmaya devam eder. Süreç kapalıysa veya güncel ağ verisi alınamıyorsa stop koruması çalışmaz.** Bunlar borsaya gönderilmiş koruyucu emirler değildir.

Farklı veritabanı için `--db /istenen/yol/paper.sqlite3` kullanabilirsiniz. Yedek alırken uygulamayı kapatıp `.data` klasörünü bütün olarak kopyalayın.

## Geçmiş test ve geliştirme

Geçmiş test aynı veri, bütçe, ücret ve kaymayla üç sabit stratejiyi değerlendirir. İlk %70 gösterge ısınması/gözlem, son %30 değerlendirmedir; parametre optimizasyonu yapılmaz. Sinyal kapanmış mumdan üretilir ve sonraki mum açılışında uygulanır. Her mum için açılış→düşük→yüksek→kapanış ve açılış→yüksek→düşük→kapanış yolları simüle edilir; kapanış net tasfiye değeri düşük olan yol seçilir. Bu iki yol bütün olası fiyat yollarını kapsamaz. Seçilen yolun ara değerleri azami düşüşe dahildir; son kapanışta açık pozisyon tasfiye edilir.

Raporda net getiri, azami düşüş, işlem sayısı, kazanma oranı, ücretler ve aynı bütçe/maliyetlerle al-tut karşılaştırması bulunur. Kısa örneklem ve sentetik verideki kazanç, strateji seçimi için yeterli kanıt değildir. Test raporu bot stratejisini kendiliğinden değiştirmez.

Geliştirme girişleri: `bot/core.py` para/risk hesabı, `bot/strategies.py` sinyaller, `bot/market.py` veri, `bot/backtest.py` test, `bot/service.py` kalıcılık/iş döngüsü ve `static/` paneldir. Parasal JSON alanları `Decimal` hassasiyetini korumak için dizge olarak iletilir. Durum `GET /api/state`, sağlık `GET /api/health`, ayarlar `PATCH /api/config`, kontroller `POST /api/control`, geçmiş test `POST /api/backtest` uçlarındadır. Sunucu yerel geliştirme içindir; varsayılan yerel dinleme adresini koruyun.

Bu uygulama bağımsız `dereli2002/AL-SAT` GitHub deposundadır. Bulut çalışma klasörü `/workspace/crypto-spot-bot` yoludur. İş ve Performans Takip uygulamasının dosyalarıyla veya geçmişiyle birleştirilmez. Başka ortama aktarırken uygulama klasörünün tamamını taşıyın; panel dosyalarını tek tek kopyalamanız gerekmez.
