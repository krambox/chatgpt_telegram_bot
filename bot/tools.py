import openai_utils
import logging
logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

def tokens(t):
    import tiktoken
    tokenizer = tiktoken.get_encoding("cl100k_base")
    return len(tokenizer.encode(t))

def yt(url):
    import re
    from bs4 import BeautifulSoup
    import requests

    soup = BeautifulSoup(requests.get(url).content,features="lxml")
    patternTitle = re.compile('(?<=title":").*(?=","lengthSeconds)')
    patternDec = re.compile('(?<=shortDescription":").*(?=","isCrawlable)')
    title = patternTitle.findall(str(soup))[0].replace('\\n','\n')
    description = patternDec.findall(str(soup))[0].replace('\\n','\n')

    from youtube_transcript_api import YouTubeTranscriptApi
    video_id = url.split("?v=")[-1] if "?v=" in url else url.split("/")[-1]
    srt = YouTubeTranscriptApi.get_transcript(video_id, languages=['de','en'])
    transcript = ""
    for chunk in srt:
        transcript = transcript + chunk["text"] + "\n"
        
    content= title + "\n\n" + description + "\n\n" + transcript
    return description,transcript 

def summarize(text,max_tokens):
    import math

    t=tokens(text)
    factor=t/max_tokens
    if factor>1:
        print("summarizing text",factor)
        #return first tokens words
        words=text.split(" ")
        firstWords=math.ceil(len(words)/factor)
        return " ".join(words[:firstWords]) 
    return text

def summarize2(text,max_tokens):
    import math
    import textwrap
    t=tokens(text)
    logger.info(f"summarizing text with {t} tokens")
    if t<max_tokens:
        return text
    factor=t/max_tokens

    chunkSize=math.ceil(len(text)/factor)
    chunks = textwrap.wrap(text, chunkSize)

    wordCount=len(text.split(" "))
    chunkWordCount=math.ceil(wordCount/factor)

    result = list()
    count = 0
    for chunk in chunks:
        logger.info(f"summarizing chunk {count+1}/{len(chunks)} with {t} tokens")
        count = count + 1
        promp=f"""
        Fasse den Text auf maximal {chunkWordCount} Wörter zusammen. Lasse keine wichtigen Informationen weg.

        Text:

        {chunk}
        """
        summary = openai_utils.complete(promp,"",chunkWordCount)
        result.append(summary)

    return summarize(" ".join(result),max_tokens)
