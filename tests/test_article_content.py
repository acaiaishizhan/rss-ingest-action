from types import SimpleNamespace
import article_extractor
from article_content import extract_body
import rss_ingest
import rss_parser

HTML='''<header>navigation</header><article id="post_1"><img src="/avatar.png">
<div class="cooked"><p>这是作品正文<br>第二行</p><a href="https://demo.example/">演示</a>
<a class="lightbox" href="https://cdn.example/original.png"><img src="/thumb.png" alt="截图"></a>
<p>图片后面仍有文字</p><script>ignore me</script></div><footer>like</footer></article>
<article id="post_2"><div class="cooked">reply should not leak</div></article>'''

def test_first_post_keeps_body_links_original_images_and_excludes_replies():
    r=extract_body(HTML,'https://linux.do/t/topic/1','discourse')
    assert '图片后面仍有文字' in r['text']
    assert 'https://demo.example/' in r['text']
    assert r['image_urls']==['https://cdn.example/original.png']
    for noise in ['navigation','ignore me','reply should not leak','avatar.png']:
        assert noise not in r['text']

def test_discourse_crawler_layout_keeps_only_first_post():
    page='<noscript><div id="post_1"><div class="post" itemprop="text"><p>首帖正文</p><img src="/shot.png"></div></div><div id="post_2"><div itemprop="text">回复</div></div></noscript>'
    r=extract_body(page,'https://linux.do/t/topic/1','discourse')
    assert '首帖正文' in r['text'] and '回复' not in r['text']
    assert r['image_urls']==['https://linux.do/shot.png']

def test_title_index_forces_first_post_fetch_even_for_long_title(monkeypatch):
    urls=[]
    def get(url,timeout):
        urls.append(url);return SimpleNamespace(status_code=200,text=HTML)
    monkeypatch.setattr(article_extractor,'_fetch_linux_do_reader',get)
    r=article_extractor.extract_article_text('https://linux.do/t/topic/1','LINUX DO','',
        {'title':'长标题'*60,'summary':'长标题'*60,'_content_incomplete':True})
    assert urls==['https://linux.do/t/topic/1']
    assert r['status']=='ok' and len(r['image_urls'])==1

def test_missing_body_is_not_success(monkeypatch):
    monkeypatch.setattr(article_extractor,'_fetch_linux_do_reader',lambda *a,**k:SimpleNamespace(status_code=200,text='<h1>Title only</h1>'))
    r=article_extractor.extract_article_text('https://linux.do/t/topic/1','LINUX DO','',{'summary':'Title only'})
    assert r['status']=='fetch_error'

def test_linux_images_are_eligible_for_attachment():
    assert rss_ingest.collect_article_image_urls({'link':'https://linux.do/t/topic/1','image_urls':['https://cdn.example/a.png']})==['https://cdn.example/a.png']

def test_index_is_explicitly_incomplete():
    feed=rss_parser._parse_jina_linux_do_html('https://linux.do/latest.rss',
        '<h3><a href="https://linux.do/t/topic/1">Title</a></h3><time>Fri, 11 Sep 2026 16:40:00 GMT</time>')
    assert feed.entries[0]['_content_incomplete'] is True
