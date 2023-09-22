# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2023 Valory AG
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# ------------------------------------------------------------------------------

"""This module implements a Mech tool for binary predictions."""

import json
import re
from datetime import datetime
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Dict, Generator, List, Optional, Tuple
from tqdm import tqdm

import openai
import requests
from bs4 import BeautifulSoup
from googleapiclient.discovery import build

from sentence_transformers import SentenceTransformer, util
from transformers import AutoTokenizer, AutoModel, BertForPreTraining, BertForMaskedLM

import spacy
import torch

NUM_URLS_EXTRACT = 5
DEFAULT_OPENAI_SETTINGS = {
    "max_tokens": 500,
    "temperature": 0.2,
}
ALLOWED_TOOLS = [
    "prediction-offline-sum-url-content",
    "prediction-online-sum-url-content",
]
TOOL_TO_ENGINE = {
    "prediction-offline-sum-url-content": "gpt-3.5-turbo",
    "prediction-online-sum-url-content": "gpt-3.5-turbo",
    # "prediction-online-sum-url-content": "gpt-4",
}

PREDICTION_PROMPT = """
You are an LLM inside a multi-agent system that takes in a prompt of a user requesting a probability estimation
for a given event. You are provided with an input under the label "USER_PROMPT". You must follow the instructions
under the label "INSTRUCTIONS". You must provide your response in the format specified under "OUTPUT_FORMAT".

INSTRUCTIONS
* Read the input under the label "USER_PROMPT" delimited by three backticks.
* The "USER_PROMPT" specifies an event.
* The event will only have two possible outcomes: either the event will happen or the event will not happen.
* If the event has more than two possible outcomes, you must ignore the rest of the instructions and output the response "Error".
* You must provide a probability estimation of the event happening, based on your training data.
* You are provided an itemized list of information under the label "ADDITIONAL_INFORMATION" delimited by three backticks.
* You can use any item in "ADDITIONAL_INFORMATION" in addition to your training data.
* Given today's date {today_date} you should use predominantly the more recent information in "ADDITIONAL_INFORMATION" to make your probability estimation.
* You must pay very close attention to the specific wording of the question in "USER_PROMPT" 
* If a date is provided in the USER_PROMPT for the event to have occured, you must also consider in your estimation, given today's date {today_date}, how likely it is that the event will occur before or on that provided date.
* If an item in "ADDITIONAL_INFORMATION" is not relevant for the estimation, you must ignore that item.
* You must provide your response in the format specified under "OUTPUT_FORMAT".
* Do not include any other contents in your response.

USER_PROMPT:
```
{user_prompt}
```

ADDITIONAL_INFORMATION:
```
{additional_information}
```

OUTPUT_FORMAT
* Your output response must be only a single JSON object to be parsed by Python's "json.loads()".
* The JSON must contain four fields: "p_yes", "p_no", "confidence", and "info_utility".
* Each item in the JSON must have a value between 0 and 1.
   - "p_yes": Estimated probability that the event in the "USER_PROMPT" occurs.
   - "p_no": Estimated probability that the event in the "USER_PROMPT" does not occur.
   - "confidence": A value between 0 and 1 indicating the confidence in the prediction. 0 indicates lowest
     confidence value; 1 maximum confidence value.
   - "info_utility": Utility of the information provided in "ADDITIONAL_INFORMATION" to help you make the prediction.
     0 indicates lowest utility; 1 maximum utility.
* The sum of "p_yes" and "p_no" must equal 1.
* Output only the JSON object first and a short explanation (max. 3 sentences) what led you to the estimation after that. Do not include any other contents in your response.
"""

URL_QUERY_PROMPT = """
You are an LLM inside a multi-agent system that takes in a prompt of a user requesting a probability estimation
for a given event. You are provided with an input under the label "USER_PROMPT". You must follow the instructions
under the label "INSTRUCTIONS". You must provide your response in the format specified under "OUTPUT_FORMAT".

INSTRUCTIONS
* Read the input under the label "USER_PROMPT", delimited by three backticks, carefully.
* The "USER_PROMPT" specifies an event.
* The event will only have two possible outcomes: either the event will happen or the event will not happen.
* If the event has more than two possible outcomes, you must ignore the rest of the instructions and output the response "Error".
* You must provide your response in the format specified under "OUTPUT_FORMAT".
* Do not include any other contents in your response.

USER_PROMPT:
```
{user_prompt}
```

OUTPUT_FORMAT
* Your output response must be only a single JSON object to be parsed by Python's "json.loads()".
* The JSON must contain two fields: "queries", and "urls".
   - "queries": An array of strings of size between 1 and 5. Each string must be a search engine query that has a high chance to yield search engine results that
     help obtain relevant information to estimate the probability that the event specified in "USER_PROMPT" occurs. You must provide original information in each query, 
     and the queries should not overlap or lead to obtain the same set of results. 
* Output only the JSON object. Do not include any other contents in your response. 
"""

def search_google(query: str, api_key: str, engine: str, num: int = 3) -> List[str]:
    service = build("customsearch", "v1", developerKey=api_key)
    search = (
        service.cse()
        .list(
            q=query,
            cx=engine,
            num=num,
        )
        .execute()
    )
    return [result["link"] for result in search["items"]]


def get_urls_from_queries(queries: List[str], api_key: str, engine: str) -> List[str]:
    """Get URLs from search engine queries"""
    results = []
    for query in queries:
        for url in search_google(
            query=query,
            api_key=api_key,
            engine=engine,
            num=3,  # Number of returned urls per query
        ):
            results.append(url)
    unique_results = list(set(results))
    
    # Remove urls that are pdfs
    unique_results = [url for url in unique_results if not url.endswith(".pdf")]
    return unique_results


def get_website_summary(text: str, prompt: str, model, tokenizer, nlp, max_words: int = 150) -> str:
    """Get text summary from a website"""    
    # Check for empty inputs
    if not prompt or not text:
        return ""

    # Calculate the BERT embedding for the prompt
    with torch.no_grad():
        question_tokens = tokenizer(prompt, return_tensors="pt", padding=True, truncation=True)
        question_embedding = model(**question_tokens).last_hidden_state.mean(dim=1)
        
    # Sentence splitting and NER
    doc = nlp(text)
    sentences = [sent.text for sent in doc.sents if len(sent.text.split()) >= 5]
    entities = [ent.text for ent in doc.ents]
    
    # Crop the sentences list to the first 300 sentences to reduce the time taken for the similarity calculations.
    sentences = sentences[:300]



    # Similarity calculations and sentence ranking
    similarities = []
    for sentence in tqdm(sentences, desc="Calculating Similarities for Sentences"):
        with torch.no_grad():
            sentence_tokens = tokenizer(sentence, return_tensors="pt", padding=True, truncation=True)
            sentence_embedding = model(**sentence_tokens).last_hidden_state.mean(dim=1)
            similarity = torch.cosine_similarity(question_embedding, sentence_embedding).item()
        if any(entity in sentence for entity in entities):
            similarity += 0.05  # Give a slight boost for sentences with entities
        similarities.append(similarity)

    # Extract the top relevant sentences
    relevant_sentences = [sent for sent, sim in sorted(zip(sentences, similarities), key=lambda x: x[1], reverse=True) if sim > 0.7]

    # Print each sentence in relevant_sentences in a new line along with its similarity score > 0.7
    for sent, sim in sorted(zip(sentences, similarities), key=lambda x: x[1], reverse=True):
        if sim > 0.7:
            print(f"{sim} : {sent}")

    # Join the top 4 relevant sentences
    output = ' '.join(relevant_sentences[:4])
    output_words = output.split(' ')
    if len(output_words) > max_words:
        output = ' '.join(output_words[:max_words])

    return output


def get_date(soup):    
    # Get the updated or release date of the website.
    # The following are some of the possible values for the "name" attribute:
    release_date_names = [
        'date', 'pubdate', 'publishdate', 'OriginalPublicationDate',
        'article:published_time', 'sailthru.date', 'article.published',
        'published-date', 'og:published_time', 'publication_date',
        'publishedDate', 'dc.date', 'DC.date', 'article:published',
        'article_date_original', 'cXenseParse:recs:publishtime', 'DATE_PUBLISHED',
        'pub-date', 'pub_date', 'datePublished', 'date_published',
        'time_published', 'article:published_date', 'parsely-pub-date',
        'publish-date', 'pubdatetime', 'published_time', 'publishedtime',
        'article_date', 'created_date', 'published_at',
        'og:published_time', 'og:release_date', 'article:published_time',
        'og:publication_date', 'og:pubdate', 'article:publication_date',
        'product:availability_starts', 'product:release_date', 'event:start_date',
        'event:release_date', 'og:time_published', 'og:start_date', 'og:created',
        'og:creation_date', 'og:launch_date', 'og:first_published',
        'og:original_publication_date', 'article:published', 'article:pub_date',
        'news:published_time', 'news:publication_date', 'blog:published_time',
        'blog:publication_date', 'report:published_time', 'report:publication_date',
        'webpage:published_time', 'webpage:publication_date', 'post:published_time',
        'post:publication_date', 'item:published_time', 'item:publication_date'
    ]

    update_date_names = [
        'lastmod', 'lastmodified', 'last-modified', 'updated',
        'dateModified', 'article:modified_time', 'modified_date',
        'article:modified', 'og:updated_time', 'mod_date',
        'modifiedDate', 'lastModifiedDate', 'lastUpdate', 'last_updated',
        'LastUpdated', 'UpdateDate', 'updated_date', 'revision_date',
        'sentry:revision', 'article:modified_date', 'date_updated',
        'time_updated', 'lastUpdatedDate', 'last-update-date', 'lastupdate',
        'dateLastModified', 'article:update_time', 'modified_time',
        'last_modified_date', 'date_last_modified',
        'og:updated_time', 'og:modified_time', 'article:modified_time',
        'og:modification_date', 'og:mod_time', 'article:modification_date',
        'product:availability_ends', 'product:modified_date', 'event:end_date',
        'event:updated_date', 'og:time_modified', 'og:end_date', 'og:last_modified',
        'og:modification_date', 'og:revision_date', 'og:last_updated',
        'og:most_recent_update', 'article:updated', 'article:mod_date',
        'news:updated_time', 'news:modification_date', 'blog:updated_time',
        'blog:modification_date', 'report:updated_time', 'report:modification_date',
        'webpage:updated_time', 'webpage:modification_date', 'post:updated_time',
        'post:modification_date', 'item:updated_time', 'item:modification_date'
    ]

    release_date = "unknown"
    modified_date = "unknown"

    # First, try to find an update or modified date
    for name in update_date_names:
        meta_tag = soup.find("meta", {"name": name}) or soup.find("meta", {"property": name})
        if meta_tag:
            modified_date = meta_tag.get("content", "")
    
    # If not found, then look for release or publication date
    for name in release_date_names:
        meta_tag = soup.find("meta", {"name": name}) or soup.find("meta", {"property": name})
        if meta_tag:
            release_date = meta_tag.get("content", "")
    
    if release_date == "unknown" and modified_date == "unknown":
        time_tag = soup.find("time")
        if time_tag:
            release_date = time_tag.get("datetime", "")

    return f"Release date {release_date}, Modified date {modified_date}"


def extract_text(
    html: str,
    prompt: str,
    model,
    tokenizer,
    nlp,
) -> str:
    """Extract text from a single HTML document"""
    # Remove HTML tags and extract text
    soup = BeautifulSoup(html, "html.parser")
    
    # Get the date of the website
    date = get_date(soup)

    # Get the main element of the website
    main_element = soup.find("main")
    if main_element:
        soup = main_element

    for script in soup(["script", "style", "header", "footer", "aside", "nav", "form", "button", "iframe"]):
        script.extract()
    text = soup.get_text()
    lines = (line.strip() for line in text.splitlines())
    chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
    text = ". ".join(chunk for chunk in chunks if chunk)
    text = re.sub(r"\.{2,}", ".", text) # Use regex to replace multiple "."s with a single ".".
    print(f">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>< TEXT: \n{text}")

    text_summary = get_website_summary(
        text=text,
        prompt=prompt,
        model=model,
        tokenizer=tokenizer,
        nlp=nlp,
    )
    return f"{date}:\n{text_summary}"


def process_in_batches(
    urls: List[str], window: int = 5, timeout: int = 10
) -> Generator[None, None, List[Tuple[Future, str]]]:
    """Iter URLs in batches."""
    with ThreadPoolExecutor() as executor:
        for i in range(0, len(urls), window):
            batch = urls[i : i + window]
            futures = [(executor.submit(requests.get, url, timeout=timeout), url) for url in batch]
            yield futures


def extract_texts(
    urls: List[str],
    prompt: str,
) -> List[str]:
    """Extract texts from URLs"""
    max_allowed = 45
    extracted_texts = []
    count = 0
    stop = False
    
    # BERT Initialization
    model = AutoModel.from_pretrained("bert-base-uncased")
    tokenizer = AutoTokenizer.from_pretrained("bert-base-uncased")

    # Spacy Initialization for NER and sentence splitting
    nlp = spacy.load("en_core_web_sm")
    
    for batch in tqdm(process_in_batches(urls=urls), desc="Processing Batches"):
        for future, url in tqdm(batch, desc="Processing URLs"):
            try:
                result = future.result()
                if result.status_code != 200:
                    continue
                extracted_text = extract_text(
                    html=result.text,
                    prompt=prompt,
                    model=model,
                    tokenizer=tokenizer,
                    nlp=nlp,
                )
                extracted_texts.append(f"{url}\n{extracted_text}")
                count += 1
                if count >= max_allowed:
                    stop = True
                    break
            except requests.exceptions.ReadTimeout:
                print(f"Request timed out: {url}.")
            except Exception as e:
                print(f"An error occurred: {e}")
        if stop:
            break
    return extracted_texts


def fetch_additional_information(
    prompt: str,
    engine: str,
    temperature: float,
    max_tokens: int,
    google_api_key: str,
    google_engine: str,
) -> str:
    """Fetch additional information."""
    url_query_prompt = URL_QUERY_PROMPT.format(user_prompt=prompt)
    moderation_result = openai.Moderation.create(url_query_prompt)
    if moderation_result["results"][0]["flagged"]:
        return ""
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": url_query_prompt},
    ]
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=messages,
        temperature=0.7,
        max_tokens=max_tokens,
        n=1,
        timeout=90,
        request_timeout=90,
        stop=None,
    )
    
    json_data = json.loads(response.choices[0].message.content)
    print(f"json_data: {json_data}")
    urls = get_urls_from_queries(
        json_data["queries"],
        api_key=google_api_key,
        engine=google_engine,
    )
    print(f"urls: {urls}")
    texts = extract_texts(
        urls=urls,
        prompt=prompt,
    )
    additional_informations = "\n\n".join(["- " + text for text in texts])
    # print(f"additional_informations: {additional_informations}")
    return additional_informations


def run(**kwargs) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Run the task"""
    print("Starting...")
    
    tool = kwargs["tool"]
    prompt = kwargs["prompt"]
    max_tokens = kwargs.get("max_tokens", DEFAULT_OPENAI_SETTINGS["max_tokens"])
    temperature = kwargs.get("temperature", DEFAULT_OPENAI_SETTINGS["temperature"])
    
    print(f"Tool: {tool}")
    print(f"Prompt: {prompt}")
    print(f"Max tokens: {max_tokens}")
    print(f"Temperature: {temperature}")

    openai.api_key = kwargs["api_keys"]["openai"]
    if tool not in ALLOWED_TOOLS:
        raise ValueError(f"Tool {tool} is not supported.")

    engine = TOOL_TO_ENGINE[tool]
    print(f"Engine: {engine}")

    additional_information = (
        fetch_additional_information(
            prompt=prompt,
            engine=engine,
            temperature=temperature,
            max_tokens=max_tokens,
            google_api_key=kwargs["api_keys"]["google_api_key"],
            google_engine=kwargs["api_keys"]["google_engine_id"],
        )
        if tool == "prediction-online-sum-url-content"
        else ""
    )

    # Get today's date and generate the prediction prompt
    today_date = datetime.today().strftime('%Y-%m-%d')
    prediction_prompt = PREDICTION_PROMPT.format(
        user_prompt=prompt, additional_information=additional_information, today_date=today_date,
    )
    print(f"prediction_prompt: {prediction_prompt}\n")

    moderation_result = openai.Moderation.create(prediction_prompt)
    if moderation_result["results"][0]["flagged"]:
        return "Moderation flagged the prompt as in violation of terms.", None
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": prediction_prompt},
    ]

    response = openai.ChatCompletion.create(
        model=engine,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        n=1,
        timeout=150,
        request_timeout=150,
        stop=None,
    )
    print(f"response: {response}")
    return response.choices[0].message.content, None
