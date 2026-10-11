# Final çalıştırma ve teslim hazırlığı

## Güncel final sözleşmesi — 11 Ekim 2026

Final tek Türkçe vakadır. Belgeler 20:00'de açılır ve teslim 23:00'ten önce
tamamlanır (UTC+3; 17:00–20:00 UTC). Güncel resmî sayfada koşu başına
30 dakika/1.800 saniye sınırı belirtilmiyor; geçmiş koşuların kendi süre
ölçümleri ve yerel bütçeleri korunur.

Builder, Markdown/Word/PDF/Excel belgelerinin bulunduğu `{case_folder}` klasörünü
ve `{mock_url}` adresini alır. Kendi Cloudflare Workers banka servisinizi
verilen servis sözleşmesi ve sentetik test müşterilerinden hazırlayın;
builder başlamadan deploy edin ve sonuçlar açıklanana kadar açık tutun.
Final telefon ajanının araçları yalnız bu servisi çağırır. Banka alanları,
kurallar ve değerler girdi belgelerinden türetilir; builder koduna veya sabit
promptlara yazılmaz. Genel hata düzeltmesi serbesttir; üretilen ajan elle
düzenlenmez.

## Belge klasöründen üretim

```text
python -m pip install -r requirements-case-reader.txt
python -B builder/build.py {case_folder} {mock_url} --out artifacts/final-builder/fresh-case --max-model-attempts 1
```

Çıktı dizini yeni olmalıdır. Mock önce yayımlanmış olmalıdır. Komut belgeleri
okur, kaynak konumlarını ve bütün dosya özetlerini kaydeder, Codex ile üretir ve
statik denetimden geçen sonucu `agent.workflow.json` olarak yazar. Bu komut
kendiliğinden Freya yayını veya simülasyon başlatmaz; canlı test ve resmî
`freya.export_workflow` çıktısı ayrıca doğrulanır.

Kaynak pakette yalnız mock/test verisi olan dosya veya klasörler
`--reference-only relative/path` ile belirtilir (birden çok kez kullanılabilir).
Bu dosyalar kaynak özetinde ve yerel okumada korunur, modelin politika girdisine
alınmaz. BRIEF.md ve README.md bu yöntemle dışlanamaz. Bu roller kaynak paketin
açıklamasından seçilir; builder içinde banka adına bağlı bir dosya kuralı yoktur.

Her model denemesinde `model-input.txt`, özgün `model-response.json` ve doğrulanmış
gerçek Codex olay akışı `model-session.jsonl` saklanır. `end-to-end-trace.json`
kontrol özetidir; teslimde istenen ajan oturum kaydının yerine geçmez. Modelin
statik kontrolü geçmesi, bankacılık kurallarının veya canlı konuşmanın geçtiği
anlamına gelmez. Yeni kaynak sürümü için yeni çıktı dizini ve yeni koşu kullanılır.

Varsayılan üretim, çözülmemiş zorunluluk varsa export etmez. Açıkça
`--allow-partial` seçildiğinde yapısal kontrollerden geçen sınırlı aday
`LOCAL_PARTIAL_CASE_WORKFLOW_EXPORTED` olarak yazılabilir; tüm açık noktalar
izde korunur. Bu seçenek kaynak, şema, adres, boyut veya bütünlük kontrollerini
atlamaz ve anlamsal/canlı başarı onayı vermez. Böyle bir adayın eksik yetkiye
bağlı işlemleri kapalı kalmalı, kullanılabilir dalları ayrıca test edilmelidir.
`--repair-from previous-output` aynı belge özeti ve mock adresine bağlı eski
yanıtı, özgün yanıt hashini doğrulayarak yeni model çağrısına onarım girdisi yapar.
Eski dosyalar değişmez; yeni koşu kendi oturumunu ve önceki girdinin bağını kaydeder.

Kaynak: https://www.freyavoice.ai/hackathon#submit ve #rules, #faq, #evaluation.

## Mevcut provider kurulumu

Python 3.10+ ve Codex CLI gerekir. Yerel üretim ve Windows varsayılan DPAPI koruması
standart kütüphane kullanır; isteğe bağlı Fernet korumasının bağımlılığı aşağıdadır.
Windows'ta kısa bir checkout dizini kullanın; çok derin klasörler varsayılan
yol uzunluğu sınırını aşabilir.
Codex CLI kendi oturumuyla çalışır; oturum dosyaları bu depoya kopyalanmaz. Jüri
makinesinde ayrıca yetkili bir Codex CLI oturumu bulunmalıdır. Bu provider başka
bir model API anahtarıyla bağlantı kurmaz.
Yerel hesabın çalışması, jüri makinesindeki model erişiminin doğrulandığı anlamına
gelmez. Codex üretim yolu prova aşamasındadır; final hazır değildir. Ölçülen CLI
sürümü `0.162.0-alpha.17.2`dir. Üretici varsayılan olarak `gpt-6.1-sol` ister;
`BUILDER_CODEX_MODEL` ile seçim değiştirilebilir. Model istemi ve bağımsız
gözlemlenen model bilgisi izde ayrı tutulur.
`BUILDER_CODEX_REASONING_EFFORT` ile `low`, `medium` veya `high` seçilebilir;
varsayılan `medium`dur. Seçilen seviye izde kayıtlıdır. Kapalı `code_mode_host`
özelliğinin ölçülmüş tek başlangıç bildirimi yalnız tam özet ve konum eşleşmesiyle
ayrılır; diğer hata ve araç olayları işlemi durdurur.

## Tarihsel iki dosyalı practice/prova komutu

```text
python -B builder/build.py path/to/brief.md path/to/tool_api.json --provider codex
```

Yeni belgeler ve kendi OpenAPI şemaları her çalışmada okunur. Builder promptunda
practice brief, cevap anahtarı veya gizli görev sabitleri bulunmaz. Kaynak
özetine tam uyan public practice API için organizatörün 11 Ekim açıklamaları
ayrıca kaynak ve kapsam bilgisiyle modele verilir. Değişmiş veya başka API için
bu açıklamalar eklenmez; yeni bankanın kuralları kendi belgelerinden üretilir.
Verilen brief'in açık zorunlulukları bu açıklamalarla geçersiz kılınmaz.
Dosya özetleri, model girdisi/çıktısı, işlem izi, süre, workflow ve manifest ayrı bir
`artifacts/final-builder/run-*` klasörüne yazılır. Bu eski iki dosyalı yolun
yerel bütçesi en fazla 1800 saniyedir; güncel resmî final sınırı değildir. Çözülemeyen politikalar, yanlış enum/alanlar ve aşırı büyük çıktılar
işlemi durdurur. İlk model çıktısı kaynak veya biçim kontrolünden geçmezse,
aynı süre bütçesinde en fazla iki otomatik düzeltme yapılır; her deneme saklanır.
Modelin kapsam beyanı anlamsal doğruluk kanıtı değildir.
Üretilen kodun davranışını bağımsız yerel senaryolarla aynı üretim döngüsünde
kontrol etmek için `--behavior-fixture path/to/behavior-fixture.json` ekleyin.
Dosya, brief ve API'nin tam SHA-256 özetlerine bağlı olmalıdır. Test girdileri ve
beklenen değerler modele verilmez. Statik kontrolden sonra gerçek üretilmiş kod
ayrı, süre sınırı olan bir yerel süreçte çalıştırılır; banka istekleri gönderilmez.
Hazırlanan gerçek istek gövdeleri, konuşma ve sonlandırma beklentileri karşılaştırılır.
Davranış hatası aynı en fazla üç üretim denemesi içinde yalnız sabit tanı kodlarıyla
otomatik düzeltmeye gider. Geçmeyen aday platforma yayımlanmaz. Sonuçlar her
denemenin `behavior-report.json` dosyasına ve üretim manifestine kaydedilir.
`LOCAL_BEHAVIOR_PASSED`, yalnız sağlanan senaryoların desteklenen yerel graph
altkümesinde geçtiğini gösterir; konuşmayı modelin doğru anlamasını, gerçek
platform zamanlamasını, banka ledger'ını veya canlı başarıyı doğrulamaz.
Desteklenmeyen graph özellikleri başarılı sayılmaz. Fixture verilmezse sonuç
`LOCAL_WORKFLOW_GENERATED` olarak kalır ve davranış testi ölçülmemiştir.
Model, düğüm ve eylemleri yapılandırılmış alanlarda verir. Bankanın araç
tanımları yalnız o çalışmanın OpenAPI şemasından oluşturulur. Kod blokları
çalıştırılmadan workflow'a yerleştirilir. Şema, kapsam, biçim ve çözülemeyen politika tanıları birlikte
otomatik düzeltmeye verilir. Çözülemeyen bir karar teslim için kabul edilmez.

İki dosyalı practice/prova yolunda platform üretimi, yayın ve export örneği:

```text
python -B builder/build.py path/to/brief.md path/to/tool_api.json --provider codex --publish --slot practice-review
```

Bir brief başka bir belgeyi açıkça temel alıyorsa, gerekli belgeleri başlıkları
ve özgün metinleri korunmuş tek kaynak dosyasında birlikte verin. Public B,
A kurallarını devralan bir ek belgedir; B provasının brief girdisi A+B içermelidir.
Builder B dosyasına A'yı otomatik eklemez. Gizli görevlerde yalnız yeni kaynakların
açıkça belirttiği belge ilişkilerini izleyin; public A/B kurallarını taşımayın.

`FREYA_API_KEY` ve gerekiyorsa `FREYA_WORKSPACE_ID` ortamda bulunmalıdır.
Windows'ta mevcut şifreli bağlantıyı yerel olarak yüklemek için
`project-management/freya-connect.ps1 -LoadOnly` kullanılabilir; bu dosya ve
şifreli anahtarlar teslim checkout'una dahil edilmez. Codex komutu bulunamıyorsa
`BUILDER_CODEX_EXECUTABLE` ile çalıştırılabilir dosyanın yolu verilebilir.

Aynı slot yeniden kullanıldığında yeni workflow açılmaz; mevcut workflow
güncellenir ve yayımlanan sürüm kontrol edilir. Yanıtı belirsiz kalan oluşturma
veya güncelleme otomatik tekrarlanmaz. Slotun önce platformdan uzlaştırılması
gerekir. Workspace kilidi mevcut test kontrolörleriyle aynı kilidi kullanır.

`provider-export.json`, kitin `freya.export_workflow` fonksiyonuyla gerçekten
platformdan alınır. Yayınlanan sürüm ve aday graph tekrar kontrol edilir. İzde
`PROVIDER_EXPORTED_TESTS_REQUIRED` görülmesi canlı testlerin geçtiği anlamına
gelmez. Bu komut kendiliğinden müşteri, persona veya simülasyon oluşturmaz.
Yayın başlamadan tam istek/yanıt kaydı hazırlanır. Varsayılan koruma Windows
kullanıcı DPAPI profilini gerektirir; koruma hazırlanamıyorsa ilk API çağrısından
önce süreç durur. Yalnız yerel üretim bu koruma katmanını kullanmaz.

Resmi public practice bankasında ayrıca bir müşteri senaryosu ölçmek için
`--practice-case PUB-01` eklenebilir. `--repetitions 3`, her tekrar için yeni
banka oturumu ve ayrı tam kanıt paketi açar; aynı yayımlanmış ajanı üç kere ölçer.
Test bu practice yolunun yerel üretim bütçesinden sayılır; bu tekrar sayısı
güncel finalin açıklanmayan puanlama sözleşmesi yerine geçmez. `FREYA_BANK_TEAM_KEY` gerekir. Bir test başarısız
olursa veya sonucu belirsizse tekrar döngüsü durur. Başka bir bankanın şemasına
public practice senaryoları uygulanmaz. Test beklentileri modele verilmez.
İsteğe bağlı `--record-credits`, her tekrarın başında ve sonunda aynı workspace'in
kredisini korunan kayıt yoluyla okur. Gözlenen fark yalnız o zaman aralığının
workspace farkıdır; testin kesin fiyatı değildir. Önceki A provasının kredi tüketimi
ölçülmemiştir. Bu bayrak olmadan kredi hesabına ek okuma yapılmaz.

Eski iki dosyalı prova yolunda, geçerli müşteri değişkenleri ve mesajları
içeren uyumlu bir test JSON'u sağlandığında ayrı metin sohbeti kontrolü yerel
üretim bütçesinde çalıştırılabilir. Bu örnek final klasör komutu değildir:

```text
python -B builder/build.py brief.md tool_api.json --provider codex --publish --slot practice-review --chat-fixture official-chat.json --chat-fixture-sha256 <independent-sha256>
```

Bu seçeneğin çalıştırılması yeni sohbet testi talebidir; önceki PUB-01 test izni
burada tekrar kullanılmaz. Test dosyasının alanları `customerVariables` ve
`messages`dır; müşteri kimliği veya senaryo builder tarafından uydurulmaz.
Sohbet kaydı ayrı korunan profile mühürlenir. Sürüm, export ve kaynaklar kontrol
edilir; ödeme/ledger, politika ve gerçek çalışma modeli ayrıca ölçülmedikçe
`NOT_MEASURED` kalır. `CHAT_CONVERSATION_CAPTURED` ödeme başarısı değildir.
Zorla sohbet kapatma, dallandırma ve belirsiz işlemi tekrarlama yapılmaz.
Resmi gizli test dosyası henüz verilmediği için bu yol canlı doğrulanmamıştır.

## Anahtarsız prova

Sınırlı ve açık politika grameri için model çağrısı gerektirmeyen yol:

```text
python -B builder/build.py tests_orchestration/inputs/single-brief.md tests_orchestration/inputs/openapi.json --provider bounded
python -B -m unittest tests_orchestration.test_contest_build tests_orchestration.test_single_markdown_sections tests_compiler.test_brief_formats -v
```

Bu yol serbest doğal dil ve tüm çok ürünlü banka politikalarını kapsamaz.

Yeni banka adresi, API alanları ve memo sınırlarını değiştiren bağımsız prova:

```text
python -B builder/build.py tests_orchestration/inputs/new_brief_behavior/juniper-new/brief.md tests_orchestration/inputs/new_brief_behavior/juniper-new/tool-api.json --provider codex --behavior-fixture tests_orchestration/inputs/new_brief_behavior/juniper-new/behavior-fixture.json
```

Bu provanın banka adresi sentetiktir; `--publish` veya gerçek banka testi için
kullanılmaz. Senaryolar kimlik öncesi gizlilik, memo tutar/tarih sınırları, açık
ödeme onayı, belirsiz yazmayı tekrar etmeme ve kaynakta belirtilen deneme sınırını
kontrol eder. Yeni bir bankanın test dosyasına bu kaynakların kuralları taşınmaz.
Bağlantı ve otomatik düzeltme kontrolleri:

```text
python -B -m unittest tests_orchestration.test_generated_behavior tests_orchestration.test_contest_behavior_pipeline -v
```

Ek olarak `tests_orchestration.test_contest_practice` ve
`tests_orchestration.test_contest_preflight` hedefli kontrolleri bulunur.
`tests_orchestration.test_model_wire`, `tests_orchestration.test_model_diagnostics`,
`tests_orchestration.test_contest_main_wire`, `tests_orchestration.test_contest_entry_evidence`,
`tests_orchestration.test_credit_observation` ve `tests_integration.test_contest_evidence`
üretici/kayıt bağlantısını ağsız sentetik girdilerle kontrol eder. Bu testlerdeki
geri çevrilebilir koruyucu gerçek şifreleme değildir; canlı banka başarısı ölçmez.
`orchestration/contest_preflight.py` seçilen kaynak commit ve temiz checkout'un
manifestini salt okunur karşılaştırır; hesap girişi veya canlı test başlatmaz.
Tam kayıt korumasının varsayılanı Windows kullanıcı DPAPI'sidir. Başka kullanıcı
veya işletim sistemi eski DPAPI kayıtlarını açamaz; hesap profili taşınmaz.
İsteğe bağlı Fernet koruması için aşağıdaki ayrı kurulum ve anahtar sözleşmesi uygulanır.

## Finalde yapılacaklar

Belgeler açılınca BRIEF.md ve belgeleri açıklayan README.md okunur. Kendi
mock servisi hazırken builder klasör ve servis adresiyle çalıştırılır. Birden
fazla koşu yapılabilir; yalnız seçilen koşunun düzenlenmemiş telefon ajanı ve
onu üreten özgün trace teslim edilir. `freya.export_workflow` ile alınan tek
export ve trace, kaynakla birlikte kesin teslim commit'ine bağlanır.

`submission.json` alanları: `team`, `members` (name/email nesnelerinin listesi),
`repo`, `commit`, `run_command`, `workflow`, `trace`, `mock_url`. Komut
`{case_folder}` ve `{mock_url}` girdilerini alır; gerçek komut ve dosya yolları
seçilen koşudan doğrulanır. Son teslimden önce resmî alıcı/yöntem kontrol edilir.

Trace başlangıç/bitiş zamanını, her model çağrısının modelini ve kısa amacını,
oluşturulan workflow/agent kimliklerini, step/tool sayısını ve yapılan testlerin
sonuçlarını göstermelidir. API anahtarları commit edilmez. Trace puanlanmaz;
ajanın builder çıktısı olduğunu denetlemek için okunur. Gizli müşteri sayısı,
başarı kriterleri ve ağırlıklar yayımlanmamıştır. En iyi adaylar yeniden
üretilebilir; model çıktısının bayt bayt aynı olması beklenmez.

11 Ekim 2026: gizli görev açılışı 20:00, son teslim 23:00 (UTC+3).
Kaynak: https://www.freyavoice.ai/hackathon#submit

## Temiz jüri ortamı ve taşınabilirlik sözleşmesi

Bu çalışma için Python 3.10+ ve gerçek Codex CLI gerekir. Yerel graph üretimi ve
Windows varsayılan DPAPI koruması Python standart kütüphanesini kullanır. Açıkça
seçilen Fernet koruması ayrıca aşağıda belirtilen isteğe bağlı bağımlılığı gerektirir.
Jüri, seçilmiş commit'i erişebildiği repodan almalı ve kendi yetkili Codex
oturumuyla çalışmalıdır. Giriş profili, token, şifreli özel kayıt veya ortam
anahtarı kaynak paketine eklenmez. `BUILDER_CODEX_EXECUTABLE`, `BUILDER_CODEX_MODEL`
ve `BUILDER_CODEX_REASONING_EFFORT` isteğe bağlı builder seçimleridir; telefon
ajanının organizatör tarafından sabitlenen model ayarını değiştirmez.

Jüri komutu final belge klasörünü ve mock adresini almalıdır. Gerçek
çalıştırılmış CLI, bağımlılıkları ve seçilen export/trace yolları teslim
commit'inde belgelenir; eski iki dosyalı örnek bu koşulu sağlamış sayılmaz.
Platform operatörü,
`FREYA_API_KEY` ve gerekirse `FREYA_WORKSPACE_ID` değerlerini jüri ortamına
güvenli biçimde sağlar. Public practice testi ayrıca `FREYA_BANK_TEAM_KEY`
gerektirir; bu anahtar ve public practice senaryoları final komutuna taşınmaz.
Final ZIP'indeki test müşterileri kendi mock'u içindir; değerlendirme
müşterileri gizlidir. Belgelenmiş chat/message testi, geçerli müşteri değişkenleri
ve mesajları içeren açıkça sağlanmış fixture ve ayrı canlı yetki gerektirir.

Varsayılan canlı kanıt koruması Windows current-user DPAPI'dir. Windows dışındaki
`--publish` çalıştırmasında Fernet açıkça seçilmeli ve aynı Python yorumlayıcısına
paketteki sabit bağımlılık kurulmalıdır:

```text
python -m pip install -r requirements-evidence-protection.txt
```

Bu dosya `cryptography==50.0.2` ister; `pyproject.toml` içinde aynı pin
`evidence-protection` isteğe bağlı grubunda da bulunur. Komutlar kaynak çalışma
dizininden çalıştırılır; paket kurulumu bütün contest CLI'larını kurmuş sayılmaz.
Fernet için operatör `FREYA_EVIDENCE_PROTECTION=fernet` ve güvenli biçimde üretilmiş,
URL-safe base64 olarak kodlanan 32 baytlık `FREYA_EVIDENCE_KEY` sağlar. Anahtar
kaynak paketi, model girdisi veya sonuç raporuna konmaz; yeni kanıtların sonraki
yerel doğrulaması için operatörün güvenli saklaması gerekir. Windows'ta da bu
açık seçim kullanılabilir. Otomatik koruyucu değişimi veya düz metne geri dönüş
yapılmaz; eksik bağımlılık, geçersiz anahtar ve çalışma sırasında değişen seçim
ilk ilgili platform isteğinden önce işlemi durdurur.

Başka kullanıcıya eski DPAPI dosyalarını veya giriş profilini taşımak çözüm
değildir. Eski DPAPI verisini okumak yine özgün Windows kullanıcı profilini
gerektirir. Testlerin sentetik koruyucusu canlı koruma sayılmaz. Yeni Fernet
testleri yalnız taze sentetik baytlar ve geçici anahtarlarla çalışır; gerçek
Linux/macOS kurulumu, jürinin işletim sistemi, oturum/model erişimi ve repoya
gerçek erişimi ayrıca ölçülmedikçe `NOT_MEASURED` kalır.

Temiz kaynak kopyasında ağsız temel doğrulama:

```text
python -B -m unittest tests_orchestration.test_model_wire tests_orchestration.test_model_diagnostics tests_orchestration.test_generated_behavior tests_orchestration.test_extraction_interface tests_orchestration.test_contest_behavior_pipeline tests_orchestration.test_contest_build tests_orchestration.test_contest_preflight tests_orchestration.test_single_markdown_sections tests_compiler.test_brief_formats tests_orchestration.test_final_run_pairing tests_orchestration.test_contest_run_pairing tests_orchestration.test_contest_checkout_closure -v
```

İsteğe bağlı bağımlılık kurulduktan sonra, taze sentetik kayıtlarla ağsız koruma
ve gönderimden önce durma sınırı doğrulanabilir:

```text
python -B -m unittest tests.test_portable_protection -v
```

Ek Cedar graph regresyonları için `GENERATED_DIALOGUE_CANDIDATE` ile incelenen
graph dosyasını açıkça seçin. Paket üreticisine `--regression-candidate` ve
bağımsız `--regression-candidate-sha256` verildiğinde bu dosya yalnız yerel test
fixture'ı olarak dahil edilir; provider export veya hidden sonuç olarak
etiketlenmez. Aday yoksa bu testler `NOT_MEASURED` açıklamasıyla atlanır;
atlanmış test davranış başarısı değildir. Bu scripted Cedar beklentileri başka
bankanın graph'ına genelleme testi olarak uygulanmaz.

Aşağıdaki eşleştirme eski iki girdili prova biçimi içindir; yeni final
klasörü/tek workflow sözleşmesi için ayrıca uyarlanıp doğrulanmalıdır.
Gerçek `contest-builder-run-1` trace'ini değiştirmeden tarihsel yerel eşleştirme için
`orchestration.final_run_pairing --trace-format contest` kullanılır. Bağımsız
reviewer selection'ı bu eski arayüzün brief/API girdilerini, kesin commit/runtime özetlerini,
original trace/manifest/FINAL/girdi/graph/export özetleri ve seçilen yayın
kimliklerini açıkça seçmelidir. Selection'ın hash'i ayrıca bağımsız alınır.

```text
python -B -m orchestration.final_run_pairing --trace-format contest --package FINAL_PAIR_DIRECTORY --selection INDEPENDENT_SELECTION_JSON --trusted-selection-sha256 INDEPENDENT_SELECTION_HASH --expected-commit EXACT_COMMIT
```

Bu doğrulayıcı özgün trace'e olay, official_final etiketi veya eksik readback
uydurmaz. Resmî girdi kaynağı ve commit beyanı bağımsız seçimden gelir. Mevcut
runner'ın publication/version kaydı builder beyanı olarak eşleşir; araç bu
kaydı bağımsız HTTP veya Git kimlik doğrulaması, canlı judge başarısı ya da
teslim tamamlandı diye sunmaz. Daha ayrıntılı readback/version tanıkları varsa
eski `--trace-format legacy` biçimi kullanılabilir. Her iki biçim yerel
denetimdir; resmî yarışma belirli bir JSON trace şeması istemez.

## Public yerel kanıt araçları

Kaynak paketi `orchestration.public39` ve `orchestration.local_pub01` için
gereken public yardımcı modülleri, public replay kaynaklarını ve
`integration/failure_learning.py` modülünü açık dosya listesiyle içerir.
Local PUB-01 doğrulayıcıları için `project-management/local-evidence` altında
yalnız `pub01_behavior.py`, `validate_pub01_v2.py`, `validate_evidence.py`,
`contract-v1.json` ve `contract-v2.json` alınır. Bu dizindeki toplanmış
kanıtlar, oturum durumu ve özel kayıtlar pakete eklenmez. Modüller ve JSON
sözleşmeleri birlikte korunmalıdır; public doğrulama koşulları hidden
bankanın beklentisi olarak kullanılamaz. Kaynak paketinin hazırlanması
yerel vakaların çalıştığı, canlı başarının ölçüldüğü veya final teslimin
tamamlandığı anlamına gelmez.
