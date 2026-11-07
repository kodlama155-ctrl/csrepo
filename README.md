# csrepo
CloudStream repo discovery and validation bot

## Adım adım tarama

Komutları `csrepo` klasöründe çalıştırın. Her aşamanın sonucu yerelde saklanır.

| Komut | Yapılan iş |
|---|---|
| `python scanner/discover.py --stage 1` | Depoları bulur. |
| `python scanner/discover.py --stage 2` | Bulunan depoları inceler; aynı anda en fazla 4 depo. |
| `python scanner/discover.py --stage 3` | Eklenti dosyalarını ve kaynak sitelere erişimi kontrol eder. |
| `python scanner/discover.py --stage 4` | Kontrol edilmiş sonuçlarla yereldeki ana listeyi günceller. |
| `python scanner/discover.py --status` | Kayıtlı aşamaları, tarihlerini ve sürelerini gösterir. |

İlk üç aşama ana listeyi değiştirmez. Örneğin, eklenti kontrolünü tekrar denemek
için yalnızca `--stage 3` çalıştırın; sonucu listeye almak için ardından `--stage 4`
çalıştırın. Aşama 3 video oynatmaz; dosya ve site erişimi kontrolü yapar.

Bir aşamayı yenilerseniz sonraki aşamalar da yeniden çalıştırılmalıdır. Başarısız
denemeler son başarılı kaydı korur. Gerekli kayıt yoksa bot hangi aşamanın önce
çalıştırılması gerektiğini söyler. Ara sonuçlar Git'e eklenmeyen `.scan-state/`
klasöründedir; dosyaları eksik veya değişmiş sonuçlar ana listeye alınmaz.

`python scanner/discover.py --from-stage 3` üçüncü aşamadan sona kadar devam eder.
`python scanner/discover.py` veya `--stage all` bütün aşamaları sırayla çalıştırır.
Bu komutlar GitHub'a gönderim yapmaz; mevcut GitHub iş akışı başarılı tam tarama
sonuçlarını ayrıca gönderir.

Doğrulanmış canlı TV eklentileri ayrıca `data/live-plugins.json` dosyasına yazılır.
Bu dosya yalnızca `tvTypes` alanında `Live` bulunan eklentileri içerir; tüketiciler
tam eklenti listesini ayrıştırmadan yalnızca bu listeyi okuyabilir.

<!-- AUTO_REPOS_START -->
## 📦 Güncel Önemli Türkçe CloudStream Repoları

Bu tablo bot tarafından otomatik güncellenir. Kısa kod/adresler kaynak repoların README dosyalarından tespit edilir.

**Son tarama:** <code>2026-11-07T05:39:46Z</code>

| Repo | Durum | Eklenti | Kısa kod / adres | Uzun repo.json |
|---|---|---:|---|---|
| [TurkSinema](https://github.com/Wiojelt/TurkSinema) | active | 107 | <code>!turksinema</code> | <code>https://raw.githubusercontent.com/Wiojelt/TurkSinema/builds/repo.json</code> |
| [WioSinema](https://github.com/Wiojelt/WioSinema) | active | 37 | <code>!wiosinema</code> | <code>https://raw.githubusercontent.com/Wiojelt/WioSinema/builds/repo.json</code> |
| [Turkish Providers Repository \| @feroxxcs3](https://github.com/feroxx/Kekik-cloudstream) | active | 37 | — | <code>https://raw.githubusercontent.com/feroxx/Kekik-cloudstream/builds/repo.json</code> |
| [MRC - CloudStream Türkçe Eklenti Deposu](https://github.com/lepotane/MRC-builds) | active | 73 | <code>https://tinyurl.com/mrc-cs</code><br><code>!mrc-cs</code> | <code>https://raw.githubusercontent.com/lepotane/MRC-builds/builds/repo.json</code> |
| [TurkSpor](https://github.com/Wiojelt/TurkSpor) | active | 45 | <code>!turkspor</code> | <code>https://raw.githubusercontent.com/Wiojelt/TurkSpor/builds/repo.json</code> |
| [CloudStream TR](https://github.com/Emre-Kahveci/CloudStreamHub) | active | 25 | — | <code>https://raw.githubusercontent.com/Emre-Kahveci/CloudStreamHub/builds/repo.json</code> |
| [Manitux Cloudstream Plugins](https://github.com/manitux-app/cs-plugins) | active | 9 | <code>!manitux-cs</code> | <code>https://raw.githubusercontent.com/manitux-app/cs-plugins/main/repo.json</code> |

Tüm doğrulanmış depolar için [catalog.md](catalog.md) dosyasına bakın.
<!-- AUTO_REPOS_END -->
