# Binance Spot deneme botu

Türkçe panelden yönetilen, birden fazla Binance Spot USDT paritesinde otomatik ve manuel **deneme alım/satımı** yapan uygulama. Tüm pozisyonlar ortak USDT bakiyesini kullanır; her pozisyonun alış fiyatı, stopu ve kâr takip zirvesi ayrıdır. İşlem bazında ve toplam net kâr/zararı, komisyonları ve açık pozisyonları gösterir. Gerçek emir göndermez; API anahtarı veya borsa hesabı gerektirmez. En yüksek kârı sağlayan veya gelecekte kâr garantileyen bir strateji iddiası yoktur.

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

**Tüm pariteler** modu, Binance kataloğunda işlem durumu `TRADING`, Spot işlem izni açık ve kotasyon varlığı USDT olan bütün pariteleri tarar. **Yalnız seçtiğim pariteler** modunda arama listesinden belirlediğiniz pariteler taranır. Sentetik demo kaynağında bu liste yalnızca 10 örnek pariteden oluşur.

Grafiğin üstündeki **varlık adı / aşağı ok** düğmesine tıklayın. Açılan listede tüm aktif USDT pariteleri görünür; arayıp bir varlığı seçtiğinizde grafik hemen o varlığa geçer. Liste klavyedeki oklar, Enter ve Escape ile de kullanılabilir. **Açık pozisyonlar** tablosunda bir satıra veya varlık adına tıklamak, sayfayı o pozisyonun grafiğine taşır. Satırdaki **Kapat** düğmesi yalnızca ilgili pozisyonu kapatır.

Grafik gerçek **OHLC mumlarını** gösterir: açılış, en yüksek, en düşük ve kapanış fiyatları; yükselen ve düşen mumların gövde ve fitilleri ayrıdır. Binance verisinde henüz kapanmamış gerçek mum ayrıca gösterilir ve kapanana kadar değişebilir. Strateji ve geçmiş test bu oluşan mumu kullanmaz. Grafik aralıkları **1 saniye, 1 dakika, 5 dakika, 15 dakika, 1 saat**; bunlar botun işlem aralığından bağımsız görüntüleme tercihleridir. 1 saniyelik seçenek yalnız grafiktedir. Veri istekleri 1 saniyelik görünümde yaklaşık her saniye, diğer aralıklarda 3–5 saniyede bir yenilenir; ağ ve API bekleme süreleri bunu uzatabilir. Eski/eksik veri açıkça işaretlenir.

Grafikte **+ / −** ile yakınlaştırıp uzaklaştırabilir, oklarla geçmiş mumlara gidebilir, **Son mumlar** ile canlı uca dönebilir, yükseklik düğmeleriyle küçültüp büyütebilir ve tam ekran açabilirsiniz. Mum üzerine gelince zaman ve OHLC değerleri görünür. Alış, sabit stop ve etkin takip stopu referansları izlenir. Yakınlaştırma, yükseklik ve bağımsız aralık seçimi otomatik yenilemede korunur.

**BTC/USDT · 5 dakika** köşe grafiği ana panelde ayrıca görünür. Seçili varlık değişse de bu grafik BTC/USDT ve 5 dakikada kalır; kendi veri kaynağı ve güncellik durumunu gösterir. BTC eğilimini izlemek içindir, otomatik işlem sinyali veya bütün altcoinlerin aynı yönde hareket edeceği anlamına gelmez.

Hem grafik başlığındaki ok hem **Manuel AL / SAT** parite alanının yanındaki ok aynı varlık listesini açar. Her görünür satırda gerçek veriden **küçük trend çizgisi, yön ve yüzde değişim** bulunur. Temel dönem satırda belirtilir: son 24 adet 5 dakikalık kapanış ve varsa güncel fiyat; geçmiş henüz yüklenmediyse gerçek 5 saniyelik fiyat gözlemleri. Yüzde bu gösterilen döneme aittir; 24 saatlik değişim veya alım/satım sinyali değildir. Yetersiz veride çizgi uydurulmaz, veri birikiyor/yükleniyor bilgisi görünür. Liste içindeki **− / +** ile önizlemeleri küçük, orta veya büyük yapabilirsiniz. Yalnız görünen satırlar yüklenir; tüm katalog için ayrı ayrı yüzlerce mum isteği gönderilmez. Varlık seçmek emir göndermez.

Grafikte seçtiğiniz parite yalnızca görüntüleme odağıdır; otomatik tarama evrenini veya açık pozisyonları değiştirmez. Farklı varlıklarda aynı anda pozisyon açılabilir; aynı paritede ikinci pozisyon veya mevcut miktara ek alış yapılmaz. Kaldıraç ve açığa satış yoktur.

Sekiz strateji bulunur: EMA 12/26 kesişimi, RSI 14 dönüşü, 20 mum kırılımı, **Karma strateji**, **ADX filtreli trend kırılımı**, **RSI BANT**, **WAP EMA** ve **SCALPİNG**. Karma stratejide EMA 12/26 trendi, RSI 14 (≤35 alış / ≥65 satış) ve önceki 20 mumun tepesi / 10 mumun dibi kırılımı oy verir. **En az iki aynı yönlü oy ve sıfır karşı oy** varsa işlem sinyali oluşur; çelişkide veya ilk 26 mum dolmadan beklenir. Koruyucu stoplar bu oylamadan bağımsızdır. Sinyaller kapanmış mumları kullanır.

**Tüm sekiz strateji için ortak otomatik alış filtresi:** **İşlem kontrolü → Hacim filtresi** anahtarından açıp kapatabilirsiniz. Yeni hesaplarda ve eski kayıtların ilk güncellemesinde açık başlar; daha önce kaydettiğiniz açık/kapalı tercihi sonraki açılışlarda korunur. Anahtar değişikliği anında kaydedilir; botu duraklatmak veya açık pozisyonları kapatmak gerekmez. **Filtre açıkken** paritenin sinyal anındaki son 24 saatlik işlem hacmi **en az 10.000.000 USDT** olmalıdır. Hacim, o ana kadar kapanmış mumların borsadan gelen gerçek **USDT hacmi** (`quote_volume`) toplanarak hesaplanır; adet hacmi veya güncel fiyatla çarpılmış tahmin kullanılmaz. Tam 24 saatlik geçmiş yoksa, mumlarda boşluk varsa veya USDT hacmi eksikse yeni otomatik alış yapılmaz. Filtre kapalıyken 10 milyon USDT sınırı ve eksik 24 saatlik hacim nedeniyle alış engeli uygulanmaz; seçili strateji koşulları, bakiye ve risk kontrolleri devam eder. Bu anahtar, stratejilerin kendi hacim göstergelerini (CMF, VWAP veya ADX stratejisinin hacim onayı) kapatmaz. Geçmiş testler kayıtlı açık/kapalı tercihini kullanır ve raporda belirtir; gerekli eski mumlar yalnızca gösterge/hacim geçmişini tamamlar, son 500 mumun değerlendirme aralığını değiştirmez. Manuel AL bu filtreye tabi değildir; mevcut pozisyonların strateji satışları, koruyucu stopları ve manuel SAT hacim nedeniyle engellenmez.

**ADX filtreli trend kırılımı** (`trend_breakout`) bir trend takip adayıdır. Alış için aynı kapanmış mumda bütün koşullar gerekir: kapanış > EMA20 > EMA50, EMA50 bir önceki muma göre yükseliyor, Wilder ADX14 ≥ 25, kapanış önceki 20 mumun en yüksek fiyatının üstünde, pozitif hacim önceki 20 mumun ortalamasının üstünde ve kapanış−EMA20 ≤ 2 × Wilder ATR14. En az 51 kapanmış mum gerekir. Kapanış EMA20 altına veya EMA20 EMA50 altına inerse satış sinyali oluşur; çıkış için hacim/ADX onayı beklenmez. Sabit stop, kâr takibi ve günlük zarar kontrolleri bağımsız çalışır. Şartlar sağlanmayınca işlem aramaz; bu seçim düşük zararı veya kazancı garanti etmez. Mevcut strateji ve mum aralığınız güncellemede otomatik değiştirilmez.


**RSI BANT** (`rsi_band`), **Bollinger Bands (20, 2)** ile **Stochastic RSI (14, 14, 3, 3)** kullanır. Bollinger orta çizgisi son 20 kapanışın basit ortalamasıdır; bantlar bu ortalamanın iki standart sapma altı ve üstüdür (standart sapma böleni 20). Stochastic RSI, Wilder RSI14 değerlerinin son 14 değerdeki konumundan hesaplanır; K ve D ayrı ayrı 3 değerlik basit ortalamalarla yumuşatılır. Alış için **aynı kapanmış mumda** en düşük fiyat alt banda eşit veya altında olmalı ve K önceki mumda ≤20 iken şimdi >20 olmalıdır. Satış için en yüksek fiyat üst banda eşit veya üstünde olmalı ve K önceki mumda ≥80 iken şimdi <80 olmalıdır. K/D kesişimi ek koşul değildir. Yeterli gösterge geçmişi veya tanımlı Stochastic RSI aralığı yoksa beklenir.

**WAP EMA** (`vwap_ema`), **9 EMA** ile günlük **VWAP** kullanır; 1 veya 5 dakikalık mumlar için eklenmiştir. VWAP, gün başından itibaren `Σ[((yüksek + düşük + kapanış) / 3) × hacim] / Σ[hacim]` olarak hesaplanır. Gün sınırı **00.00 UTC**, yani **Türkiye saatiyle 03.00** olarak sabittir. Önceki kapanış iki çizginin birden üstünde değilken yeni kapanış ikisinin de üstüne geçerse alış; önceki kapanış iki çizginin birden altında değilken yeni kapanış ikisinin de altına geçerse satış oluşur. İki çizginin ayrı ayrı aynı mumda kesilmesi gerekmez. VWAP sıfırlanmasının tek başına sinyal üretmemesi için yeni günün ilk mumunda beklenir. Gün başlangıcından itibaren eksiksiz mum geçmişi yoksa veya sinyal mumunda hacim sıfırsa işlem sinyali üretilmez. VWAP, o paritenin hacim ağırlıklı fiyat ortalamasıdır; belirli yatırımcıların maliyetini göstermez.


**SCALPİNG** (`scalping`), **EMA20**, **Chaikin Money Flow (CMF20)** ve **Stochastic RSI (14, 14, 3, 3)** kullanır. Aynı kapanmış mumda kapanış EMA20'nin üstünde, CMF20 pozitif, önceki K ≤ önceki D, yeni K > yeni D ve yeni K ile D'nin **ikisi de 20'nin altında** olursa alış sinyali oluşur. Hacim filtresi açıkken işlem açılması için ortak 24 saatlik hacim şartı da sağlanmalıdır. CMF20 son 20 mum için `Σ[(((kapanış − düşük) − (yüksek − kapanış)) / (yüksek − düşük)) × hacim] / Σ[hacim]` olarak hesaplanır; yüksek ve düşük eşitse o mumun katkısı sıfırdır. Toplam hacim sıfırsa veya göstergeler hazır değilse beklenir. Pozitif CMF, hacimle ağırlıklandırılmış pozitif kapanış baskısını ifade eder; piyasaya net yeni para girişinin kanıtı değildir. Bu stratejinin ayrı bir gösterge satış sinyali yoktur; çıkışları mevcut sabit stop, kâr takibi ve manuel SAT yönetir.

Bu üç strateji de kapanmış mumlardan sinyal üretir. Binance Spot sürümünde satış sinyali yalnızca mevcut pozisyonu kapatır; short pozisyon açılmaz. Satış sinyali işlemin kârda olduğunu şart koşmaz. Stoplar, kâr takibi ve komisyon/kayma hesapları bağımsız çalışır. Yeni stratejiler mevcut kayıtlı stratejiyi veya ayarları otomatik değiştirmez; aynı veride maliyetlerden sonra karşılaştırılacak adaylardır.

RSI stratejisinde düşük RSI tek başına yükseliş onayı değildir: RSI ≤30 kaldığı sürece sonraki kapanmış mumlarda yeniden alış sinyali gelebilir. Özellikle kısa aralıklarda maliyetler sonucu etkiler; varsayılan %0,10 + %0,10 komisyon ve iki yönlü %0,05 kayma, değişmeyen fiyatta yaklaşık %0,30 gidiş-dönüş maliyetidir. Yeni seçeneği aynı veride, aynı maliyetlerle diğer yöntemlerle karşılaştırın; 15 dakika veya 1 saat aralıklarını ayrıca inceleyebilirsiniz. Kısa örneklemde yüksek getiri gelecekteki performansı kanıtlamaz.

Aşağıdaki değerler yeni oluşturulan deneme veritabanı içindir; kayıtlı ayarlar güncellemede korunur.

| Ayar | Varsayılan |
| --- | --- |
| Tarama evreni | Tüm aktif Binance Spot USDT pariteleri |
| Grafik odağı / mum / strateji | BTCUSDT / 5 dakika / Karma strateji |
| En fazla açık pozisyon | 5; ortak USDT bakiyesine bağlı |
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

Asıl varsayılanlar `bot/core.py` içindeki `DEFAULT_CONFIG` ve `bot/service.py` içindeki `APP_DEFAULTS` değerleridir. Grafik odağı çalışırken değiştirilebilir. Diğer ayarlar için botu duraklatın ve açık deneme pozisyonlarını kapatın. İşlem geçmişi oluştuktan sonra veri kaynağı veya başlangıç bakiyesi değişikliği için deneme defterini sıfırlayın. Sıfırlama önceki deneme bakiyesini ve görünür işlem geçmişini temizler.

Fiyat döngüsü her turdan sonra 5 saniye bekler; ağ yanıt süresi bu aralığı uzatabilir. Tek bir toplu fiyat isteğiyle gelen güncel fiyatlarda bütün açık pozisyonların stopları, mum taraması ve yeni strateji işlemlerinden önce değerlendirilir. Mumlar kademeli taranır: en fazla 12 bekleyen iş, 4 çalışan ve açık pozisyonlara öncelik vardır. Geniş bir evrenin tamamının aynı anda veya her 5 saniyede tarandığı varsayılmamalıdır. Panel durumu 3 saniyede bir yeniler; taranan ve bekleyen parite sayıları panelde görünür. Borsa istek sınırına ulaşıldığında belirtilen bekleme süresine uyulur.

**Komisyonlar tahmindir.** Alanlarda `0.10`, **%0,10** anlamına gelir. Binance hesabının güncel komisyonu otomatik sorgulanmaz; hesap seviyesi, kampanya ve BNB indirimleri uygulanmaz. Ücretlerin USDT ödendiği varsayılır. Kayma alış fiyatını yükseltir, satış fiyatını düşürür. Binance miktar adımı ile uygulanabilir minimum/maksimum miktar ve emir tutarı kuralları alışa uygulanır; emir defteri derinliği ve kısmi gerçekleşmeler modellenmez.

Gerçekleşmiş kâr/zarar, net satış geliri eksi alış maliyetidir; alış ve satış ücretleri dahildir. Açık kâr/zarar, pozisyonların satış ücreti ve kayma sonrası tahmini tasfiye değerinden hesaplanır. Toplam, gerçekleşmiş ve açık kâr/zararın toplamıdır. İşlem getiri yüzdesi ilgili alış maliyetine, toplam getiri yüzdesi portföyün başlangıç bakiyesine göredir.

## Anlık kâr/zarar ve açık pozisyon penceresi

Üstte **Toplam net kâr / zarar** kartının yanında **Anlık net kâr / zarar** bulunur. Bu kart yalnız açık pozisyonların alış maliyeti ve tahmini satış maliyetleri sonrası net tasfiye sonucunu toplar; kapanan işlemlerin kârını içermez. Yüzde, açık pozisyonların toplam komisyon dahil alış maliyetine göredir; pozisyon yüzdelerinin basit ortalaması değildir. Açık işlem yoksa sıfırdır. Panel 3 saniyede bir durumu okur; fiyat döngüsünün/ağın hızı yeni fiyatın gelişini belirler. Eksik/gecikmiş kotasyonda veya sunucu bağlantısı kesilince anlık tutar ve yüzde `—` olur, açıklama görünür. Toplam hesap değeri son bilinen değerlemeyi içerebilir.

Sol menüdeki **Açık pozisyonlar** veya tablodaki **Tüm grafikleri aç ↗**, ayrı bir tarayıcı penceresinde `/positions.html` ekranını açar. Tarayıcı tercihinize göre yeni sekme de kullanılabilir. Bütün açık pozisyonların grafikleri, alış ve stop bilgileri, net kâr/zararı birlikte izlenir. Her kartta **SAT / Kapat** ilgili miktarın tamamını satar; grafik seçmek emir göndermez. Bu ekranda manuel alış, duraklatma, normal tümünü kapatma ve kalıcı acil durdurma da bulunur. Ekran açıkken fiyat/stop ve güncelleme takibi sürer; stopları bekletme veya otomasyonu kendiliğinden yeniden başlatma işlemi yapılmaz. Ayrı pencereyi kapatmak sunucuyu veya botu durdurmaz.

## Manuel AL / SAT ve acil durdurma

**Manuel AL / SAT**, otomasyon açıkken de kullanılabilir. Pariteyi seçip alış için komisyon dahil USDT bütçesini girin. **SAT**, seçili paritedeki açık miktarın tamamını kapatır; kısmi satış yapılmaz. Manuel işlemler de ortak bakiye, açık pozisyon sayısı, borsa filtreleri ve günlük zarar sınırına tabidir. Güncel fiyat olmadan işlem yapılmaz. Manuel AL/SAT sonrasında ilgili paritede, işlemden sonra kapanacak yeni mum gelene kadar eski mum sinyali yeniden kullanılmaz; sabit stop, kâr takibi ve günlük risk kontrolleri sürer.

**ACİL DURDUR / TÜMÜNÜ KAPAT**, önce otomasyonu durdurur ve kalıcı acil kilidini kaydeder. Sonra güncel fiyatı olan deneme pozisyonlarını kapatır. Fiyatı eski veya eksik olan pozisyonlar kapanmış sayılmaz; panelde bekleyen olarak görünür ve güncel fiyat akışı döndüğünde kapatma yeniden denenir. Bu sırada normal koruyucu çıkışlar da çalışır. Uygulama yeniden başlatılsa da acil kilidi korunur.

Acil kilit açıkken **Başlat** ve manuel **AL** engellenir. Her acil durdurma kalıcı bir sürüm numarası alır; daha eski bir pencereden gecikerek gelen kilit kaldırma isteği yeni acil durdurmayı kaldıramaz. Bütün pozisyonlar kapandıktan sonra **Acil kilidini kaldır** ile kilidi açıkça kaldırın. Bot duraklatılmış kalır; otomasyonu yeniden açmak için ayrıca **Başlat** gerekir. Günlük zarar kilidi gibi diğer risk sınırları bu işlemle kaldırılmaz.

## Stoplar ve kalıcı kayıt

Sabit stop, seçili takip moduyla birlikte çalışır. `stop_loss_pct=0` sabit stopu, `daily_loss_limit_pct=0` günlük limiti, `trailing_mode=off` kâr takibini kapatır.

- **Zirve fiyattan geri çekilme:** Zirve 110 USDT, mesafe %2 ise satış eşiği 107,80 USDT'dir. Zirve yükseldikçe eşik yükselir; aşağı gevşemez. Bu modda eşik komisyon sonrası pozitif kârı tek başına garanti etmez.
- **Net kârdan geri verme:** Komisyon ve kayma sonrası görülen en yüksek kâr 10 USDT, geri verme %25 ise net kâr 7,50 USDT'ye indiğinde çıkış tetiklenir. Bu, fiyatın %25 düşmesi anlamına gelmez.

Takip modu, net kâr aktivasyon eşiğine ulaşıldıktan sonra devreye girer. Koruyucu çıkışlar strateji sinyalinden önce değerlendirilir; aynı fiyat gözleminde stop sonrası yeniden alış yapılmaz. Stoplar gözlenen fiyat üzerinden simüle edilir; fiyat boşluğunda stop eşiğinden gerçekleşme garantisi yoktur.

Her varlığın sabit stopu ve kâr takibi bağımsızdır. Günlük zarar sınırı ise ortak portföyün toplam özkaynağına uygulanır; açık zarar ve ücretleri de kapsar. Aşılınca güncel fiyatı olan pozisyonlar kapatılır, fiyatı eksik olanlar güncel veri gelene kadar risk çıkışı için bekler ve yeni alışlar kilitlenir. Yeni gün İstanbul saatine göre başlar; taşınan pozisyonlarda günlük başlangıç, tüm açık pozisyonların fiyatının bulunduğu ilk başarılı gözlemdir. Bekleyen risk çıkışları tamamlanmadan kilit erken kaldırılmaz.

Veriler varsayılan olarak **`.data/paper.sqlite3`** içinde saklanır. Yeniden başlatmada ortak bakiye, işlemler, açık pozisyonlar, takip zirveleri, günlük kilit ve varsa acil kilidi geri yüklenir; yeni strateji işlemleri duraklatılmış başlar. **Duraklatma sırasında fiyat takibi ve açık pozisyon stopları çalışmaya devam eder. Süreç kapalıysa veya ilgili varlığın güncel fiyatı alınamıyorsa o pozisyonun stopu çalışmaz.** Bunlar borsaya gönderilmiş koruyucu emirler değildir.

Bir açık pozisyonun fiyatı eksikse yeni alışlar engellenir; güncel fiyatı gelen diğer pozisyonların koruyucu kontrolleri sürer. Ekranda son bilinen fiyatla gösterilen değerler güncel işlem fiyatı sayılmaz; eski fiyatla satış yapılmaz.

Farklı veritabanı için `--db /istenen/yol/paper.sqlite3` kullanabilirsiniz. Yedek alırken uygulamayı kapatıp `.data` klasörünü bütün olarak kopyalayın.

## Eski sürümden güncelleme

Eski tek pozisyonlu v1 SQLite kaydı ilk açılışta otomatik olarak çoklu portföye aktarılır. Önce aynı dizinde `*.v1-backup-*.sqlite3` biçiminde bir SQLite yedeği alınır. Nakit, işlem geçmişi, ücretler, açık pozisyon, takip zirveleri ve günlük risk durumu korunur; otomasyon duraklatılmış açılır. Eski strateji ayarı EMA ise korunur. Karma stratejiye geçmek için pozisyonlar kapalı ve bot duraklatılmışken ayarlardan **Karma strateji** seçin.

Windows'ta en kolay güncelleme yolu:

1. Eski botun konsolunda `Ctrl+C` ile uygulamayı durdurun ve konsolu kapatın.
2. Yeni ZIP'i yeni bir klasöre çıkarın.
3. Yeni botu açmadan önce eski klasördeki **`.data` klasörünün tamamını** yeni uygulama klasörüne kopyalayın. Kopyalama sırasında iki uygulama da kapalı olmalıdır.
4. Yeni klasördeki **BASLAT.bat** dosyasını açın; kayıt aktarımından sonra ayarları ve pozisyonları kontrol edin.

Aynı klasörde güncelliyorsanız önce konsolu kapatın, kod dosyalarını yenileyin ve mevcut `.data` klasörünü koruyun. Çalışan uygulamanın veritabanı üzerine dosya kopyalamayın. Windows başlatıcısı bu Linux bulut ortamında çalıştırılmamıştır.

## Geçmiş test ve geliştirme

Geçmiş test, grafikte odaklanılan **tek parite** için aynı veri, bütçe, ücret ve kaymayla RSI BANT, WAP EMA ve SCALPİNG dahil sekiz sabit stratejiyi değerlendirir; çoklu portföy testi değildir. İlk %70 gösterge ısınması/gözlem, son %30 değerlendirmedir; parametre optimizasyonu yapılmaz. Sinyal kapanmış mumdan üretilir ve sonraki mum açılışında uygulanır. Her mum için açılış→düşük→yüksek→kapanış ve açılış→yüksek→düşük→kapanış yolları simüle edilir; kapanış net tasfiye değeri düşük olan yol seçilir. Bu iki yol bütün olası fiyat yollarını kapsamaz. Seçilen yolun ara değerleri azami düşüşe dahildir; son kapanışta açık pozisyon tasfiye edilir.

Raporda net getiri, azami düşüş, işlem sayısı, kazanma oranı, ücretler ve aynı bütçe/maliyetlerle al-tut karşılaştırması bulunur. Kısa örneklem ve sentetik verideki kazanç, strateji seçimi için yeterli kanıt değildir. Test raporu bot stratejisini kendiliğinden değiştirmez.

Geliştirme girişleri: `app.py` HTTP sunucusu, `bot/portfolio.py` ortak bakiye ve portföy riskleri, `bot/core.py` pozisyon stopları ve para hesabı, `bot/strategies.py` sinyaller, `bot/market.py` katalog/fiyat/mum verisi, `bot/backtest.py` tek parite testi, `bot/service.py` kalıcılık ve tarama döngüsü, `static/` paneldir. Parasal JSON alanları `Decimal` hassasiyetini korumak için dizge olarak iletilir. Durum `GET /api/state`, pariteler `GET /api/symbols`, grafik geçmişi `GET /api/chart?symbol=ETHUSDT&interval=5m`, toplu mini trendler `GET /api/trends?symbols=BTCUSDT,ETHUSDT`, sağlık `GET /api/health`, ayarlar `PATCH /api/config`, kontroller `POST /api/control`, geçmiş test `POST /api/backtest` uçlarındadır. Manuel kontroller `{"action":"buy","symbol":"BTCUSDT","amount":"100"}` ve `{"action":"sell","symbol":"BTCUSDT"}`; acil durdurma `{"action":"emergency_close"}` gövdesini kullanır. `clear_emergency` eyleminde `expected_emergency_revision` alanı, son `GET /api/state` yanıtındaki `emergency.revision` tamsayısını içermelidir; panel bu değeri otomatik iletir. Sunucu yerel geliştirme içindir; varsayılan yerel dinleme adresini koruyun.

Bu uygulama bağımsız `dereli2002/AL-SAT` GitHub deposundadır. Bulut çalışma klasörü `/workspace/crypto-spot-bot` yoludur. İş ve Performans Takip uygulamasının dosyalarıyla veya geçmişiyle birleştirilmez. Başka ortama aktarırken uygulama klasörünün tamamını taşıyın; panel dosyalarını tek tek kopyalamanız gerekmez.
