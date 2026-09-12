"""Extract source body and its links/media without page chrome or forum replies."""
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

VOID = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}

class BodyParser(HTMLParser):
    def __init__(self, url, mode='fragment'):
        super().__init__(convert_charrefs=True)
        self.url, self.mode = url, mode
        self.stack, self.parts, self.images, self.links = [], [], [], []
        self.depth = 0 if mode == 'fragment' else None
        self.finished = False

    def active(self):
        # Discourse's crawler page itself lives inside <noscript>; only suppress
        # executable/hidden descendants of the selected body, not its ancestors.
        return self.depth is not None and not self.finished and not any(t in {'script','style','noscript','svg'} for t,a in self.stack[self.depth:])

    def public_link(self, value):
        url=urljoin(self.url,value or '')
        return url if url.startswith(('https://','http://')) else ''

    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag not in VOID: self.stack.append((tag,a))
        if self.depth is None and not self.finished:
            if self.mode=='discourse':
                first=any(v.get('id')=='post_1' for _,v in self.stack)
                selected=first and ('cooked' in (a.get('class') or '').split() or a.get('itemprop')=='text')
            else:
                selected=tag=='article'
            if selected: self.depth=len(self.stack)
        if not self.active(): return
        if tag in {'p','div','br','li','h1','h2','h3','blockquote'}: self.parts.append('\n')
        if tag=='a':
            href=self.public_link(a.get('href'))
            if href:
                self.links.append(href)
                self.parts.append(' ('+href+') ')
        if tag=='img':
            # Discourse lightboxes link to originals; don't save both original and thumbnail.
            parent=next((v.get('href') for t,v in reversed(self.stack) if t=='a' and 'lightbox' in (v.get('class') or '').split()),None)
            src=self.public_link(parent or a.get('data-src') or a.get('src'))
            if src:
                self.images.append(src)
                self.parts.append('\n[图片 '+(a.get('alt') or '')+'] '+src+'\n')
        if tag in {'video','source','iframe'}:
            src=self.public_link(a.get('src'))
            if src: self.links.append(src);self.parts.append('\n[媒体] '+src+'\n')

    def handle_endtag(self, tag):
        if tag in VOID: return
        index=next((i for i in range(len(self.stack)-1,-1,-1) if self.stack[i][0]==tag),None)
        if index is None: return
        if self.active() and tag in {'p','div','li','blockquote'}: self.parts.append('\n')
        if self.depth is not None and self.depth>0 and index+1<=self.depth: self.finished=True
        del self.stack[index:]

    def handle_data(self,data):
        if self.active(): self.parts.append(data)

    def result(self):
        body=re.sub(r'[ \t]+',' ',''.join(self.parts))
        body=re.sub(r'\n\s*\n+','\n\n',body).strip()
        return {'text':body,'image_urls':list(dict.fromkeys(self.images)), 'links':list(dict.fromkeys(self.links))}

def extract_body(raw_html,url,mode='fragment'):
    p=BodyParser(url,mode);p.feed(raw_html or '');p.close();return p.result()
