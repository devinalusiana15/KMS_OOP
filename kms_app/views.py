import os
from datetime import datetime
from collections import defaultdict

import fitz
from nltk.tokenize import word_tokenize
from nltk.corpus import stopwords
from nltk.tag import pos_tag

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.shortcuts import render, redirect, get_object_or_404
from django.utils.safestring import mark_safe

from .forms import LoginForm, UploadFileForm
from .models import (
    nlp_default,
    nlp_custom,
    merge_entities,
    get_fuseki_data,
    Uploader,
    Documents,
    Terms,
    PostingLists,
    DocDetails,
    Refinements,
    TermLemmas,
    PostingListLemmas
)

def login(request):
    if request.method == 'POST':
        form = LoginForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data['username']
            password = form.cleaned_data['password']
            try:
                user = Uploader.objects.get(username=username)
                if user.password == password:
                    request.session['uploader_id'] = user.uploader_id
                    return redirect('uploadKnowledge')
                else:
                    form.add_error(None, 'Invalid username or password')
            except Uploader.DoesNotExist:
                form.add_error(None, 'Invalid username or password')
    else:
        form = LoginForm()
    return render(request, 'pages/uploaders/login.html', {'form': form})

def logout(request):
    del request.session['uploader_id']
    return redirect('login')

def addKnowledge(request):
    return render(request, 'pages/seekers/addKnowledge.html')

def uploadKnowledge(request):
    if request.method == 'POST':
        form = UploadFileForm(request.POST, request.FILES)
        if form.is_valid():
            uploaded_file = request.FILES['file']
            if uploaded_file.content_type != 'application/pdf':
                messages.error(request, 'File must be in PDF format.')
            else:
                upload_dir = os.path.join(settings.BASE_DIR, 'kms_app/uploaded_files')
                if os.path.exists(os.path.join(upload_dir, uploaded_file.name)):
                    messages.error(request, 'File already exists.')
                else:
                    new_document = Documents(document_name=uploaded_file.name, document_path='kms_app/uploaded_files/'+uploaded_file.name)
                    new_document.save()
                    
                    handle_uploaded_file(uploaded_file)                    
                    create_and_save_inverted_index(new_document)

                    extract_text = extract_text_from_pdf(new_document.document_path)
                    text = extract_text.replace('\n', ' ')
                    document = [merge_entities(nlp_custom(sentence)) for sentence in text.split('.') if sentence.strip()]
                    
                    print(document)
                    ontology = generate_ontology(document)
                    save_ontology(ontology, new_document.document_name.replace('.pdf', '.owl'))

                    
                    messages.success(request, 'New knowledge is added successfully')
                    return render(request, 'pages/uploaders/uploadersAddKnowledge.html')
        else:
            messages.error(request, 'Failed to add new knowledge')
    else:
        form = UploadFileForm()
    return render(request, 'pages/uploaders/uploadersAddKnowledge.html', {'form': form})

def pos_tagging_and_extract_verbs(text):
    tokens = word_tokenize(text)
    stop_words = set(stopwords.words('english'))
    pos_tags = pos_tag(tokens)
    verbs = [word for word, pos in pos_tags if pos.startswith('VB') and word.lower() not in stop_words]
    return verbs

def pos_tagging_and_extract_nouns(text):
    not_include = "coffee"
    tokens = word_tokenize(text)
    pos_tags = pos_tag(tokens)
    nouns = [word for word, pos in pos_tags if pos.startswith('NN') and word != not_include]
    return nouns

def pos_tagging_and_extract_nouns_ontology(text):
    not_include = ["coffee", "definition"]
    tokens = word_tokenize(text)
    pos_tags = pos_tag(tokens)
    nouns = [word for word, pos in pos_tags if pos.startswith('NN')]

    if len(nouns) == 1 and nouns[0] == "coffee":
        return nouns
    else:
        nouns = [noun for noun in nouns if noun not in not_include]
        return nouns

def find_answer_type(question):

    question = question.lower().split()

    format = ['what', 'when', 'where', 'who', 'why', 'how']

    if question[0] in format:
      if 'where' in question:
          return ['LOC', 'GPE', 'CONTINENT', 'LOCATION']
      elif 'who' in question:
          return ['NORP', 'PERSON','NATIONALITY']
      elif 'when' in question:
          return ['DATE', 'TIME']
      elif 'what' in question:
          if 'definition' in question:
            return ['definition']
          else:
            return ['PERCENT', 'PRODUCT', 'VARIETY', 'METHODS', 'BEVERAGE', 'QUANTITY']
      elif 'how' in question:
          return ['direction']
    else:
        return "Pertanyaan tidak valid"

def find_answer(answer_types, entities):
    answer_types_mapping = {
        'LOC': ['LOC','GPE', 'CONTINENT'],
        'PERSON': ['NORP', 'PERSON','NATIONALITY', 'JOB'],
        'DATE': ['DATE', 'TIME'],
        'PRODUCT': ['PRODUCT', 'VARIETY', 'METHODS', 'BEVERAGE', 'QUANTITY', 'DISTANCE', 'TEMPERATURE'],
    }
    for ent_text, ent_label in entities:
        for answer_type, labels in answer_types_mapping.items():
            if answer_type in answer_types and ent_label in labels:
                return ent_text
    return "Tidak ada informasi yang ditemukan."

def lemmatization(text):
    doc = nlp_default(text)
    filtered_tokens = [token.lemma_ for token in doc if not token.is_stop and not token.is_punct]
    return filtered_tokens

def retrieve_documents(keywords=None, nouns=None):
    relevant_documents = []
    relevant_sentences = []
    
    if keywords is None and nouns is None:
        return relevant_documents, relevant_sentences  # Mengembalikan dua nilai
    
    terms = Terms.objects.none()
    if keywords is not None:
        terms = Terms.objects.filter(term__in=keywords)
    if nouns is not None:
        terms = terms | Terms.objects.filter(term__in=nouns)
    
    if terms.exists():
        posting_entries = PostingLists.objects.filter(term__in=terms)
        for entry in posting_entries:
            doc_detail = entry.docdetail
            document_content = DocDetails.objects.filter(docdetail_id=doc_detail.docdetail_id).values_list('docdetail', flat=True).first()
            relevant_sentence = document_content
            
            # Kalau di luar for nanti related articlenya bakal cuma satu
            relevant_documents.append({
                'detail': entry.docdetail.docdetail_id,
                'document_name': entry.docdetail.document_id,
                'context': document_content,
                'relevant_sentence': relevant_sentence,
                'url': f'/document/{doc_detail.document_id}'
            })
        relevant_sentences.append(relevant_sentence)
    
    return relevant_documents, relevant_sentences

def retrieve_documents_lemmas(keywords=None, nouns=None):
    relevant_documents = []
    relevant_sentences = []

    if keywords is None and nouns is None:
        return relevant_documents, relevant_sentences

    terms_lemma = TermLemmas.objects.none()
    if keywords is not None:
        terms_lemma = TermLemmas.objects.filter(termlemma__in=keywords)
    if nouns is not None:
        terms_lemma = terms_lemma | TermLemmas.objects.filter(termlemma__in=nouns)

    if terms_lemma.exists():
        posting_entries = PostingListLemmas.objects.filter(termlemma__in=terms_lemma)
        for entry in posting_entries:
            doc_detail = entry.docdetail
            document_content = DocDetails.objects.filter(docdetail_id=doc_detail.docdetail_id).values_list('docdetail', flat=True).first()
            relevant_sentence = document_content

            relevant_documents.append({
                'detail': entry.docdetail.docdetail_id,
                'document_name': entry.docdetail.document_id,
                'context': document_content,
                'relevant_sentence': relevant_sentence,
                'url': f'/document/{doc_detail.document_id}'
            })
            relevant_sentences.append(relevant_sentence)

    return relevant_documents, relevant_sentences

def get_answer_new(question):
    keywords_verbs = pos_tagging_and_extract_verbs(question)
    keywords_nouns = pos_tagging_and_extract_nouns(question)
    response_text = f"Pertanyaan asli: {question}<br>Keywords (Verbs): {keywords_verbs}<br>Keywords (Nouns): {keywords_nouns}<br>"
    
    answer = "Tidak ada informasi yang ditemukan."
    
    search_result_verbs, relevant_sentences_verbs = retrieve_documents(keywords=keywords_verbs)
    
    if not search_result_verbs:
        search_result_nouns, relevant_sentences_nouns = retrieve_documents(nouns=keywords_nouns)
        search_result_verbs.extend(search_result_nouns)
        relevant_sentences_verbs.extend(relevant_sentences_nouns)
    
    if not search_result_verbs:
        lemmatized_verbs = lemmatization(' '.join(keywords_verbs))
        lemmatized_nouns = lemmatization(' '.join(keywords_nouns))

        search_result_lemmas_verbs, relevant_sentences_lemmas_verbs = retrieve_documents_lemmas(keywords=lemmatized_verbs)

        if not search_result_lemmas_verbs:
            search_result_lemmas_nouns, relevant_sentences_lemmas_nouns = retrieve_documents_lemmas(nouns=lemmatized_nouns)
            search_result_lemmas_verbs.extend(search_result_lemmas_nouns)
            relevant_sentences_lemmas_verbs.extend(relevant_sentences_lemmas_nouns)

        search_result_verbs.extend(search_result_lemmas_verbs)
        relevant_sentences_verbs.extend(relevant_sentences_lemmas_verbs)

    if search_result_verbs:
        for i, result in enumerate(search_result_verbs):
            doc_content = result['relevant_sentence']
            doc_entities = merge_entities(nlp_default(doc_content)).ents
            print(f"Entities in document {result['document_name']}: {doc_entities}")

            answer_types = find_answer_type(question)
            print(f"Answer types: {answer_types}")

            answer = find_answer(answer_types, [(ent.text, ent.label_) for ent in doc_entities])
            print(f"Answer found: {answer}")

            if answer != "Tidak ada informasi yang ditemukan.":
                response_text += f"<br>Jawaban: {answer}"
                break
            else:
                response_text += f"<br>Jawaban tidak ditemukan dalam dokumen: {result['document_name']}"
                refine = Refinements(question=question, answer=answer)
                refine.save()
                answer = "Tidak ada informasi yang ditemukan."
    else:
        response_text += "<br>Dokumen yang relevan tidak ditemukan."
        refine = Refinements(question=question, answer=answer)
        refine.save()

    context = {'response_text': response_text, 'related_articles': relevant_sentences_verbs}
    print(context)
    extra_info = get_extra_information(answer)
    return answer, search_result_verbs, extra_info


def home(request):
    if request.method == 'POST':
        # search_query = request.POST.get('question')
        search_query = request.POST.get('question')
        print({"Pertanyaan: ", search_query})
        answer_types = find_answer_type(search_query)
        annotation_types = ['definition', 'direction']
        if not any(answer_type in annotation_types for answer_type in answer_types):
            answer_context, related_articles, extra_info = get_answer_new(search_query)
            print('MASUK ATAS')
            context = {
                'question': search_query,
                'answer': answer_context,
                'related_articles': related_articles,
                'extra_info': extra_info
            }
        else:
            answer = get_annotation(search_query, answer_types)
            print('MASUK BAWAH')
            context = {
                'question': search_query,
                'answer': mark_safe(answer),
                'related_articles': None,
                'extra_info': None
            }
        print(f'ini context related article: {context}')
        return render(request, 'Home.html', context)
    else:
        return render(request, 'Home.html', {'related_articles': []})

def handle_uploaded_file(file):
    upload_dir = os.path.join(settings.BASE_DIR, 'kms_app/uploaded_files')
    if not os.path.exists(upload_dir):
        os.makedirs(upload_dir)
    with open(os.path.join(upload_dir, file.name), 'wb+') as destination:
        for chunk in file.chunks():
            destination.write(chunk)

def extract_text_from_pdf(context_path):
    text = ""
    try:
        with fitz.open(context_path) as doc:
            for page in doc:
                text += page.get_text()
    except Exception as e:
        print("Error:", e)
    return text

def extract_text_from_pdf_onto(context_path):
    text = ""
    try:
        with fitz.open(context_path) as doc:
            for page in doc:
                text += page.get_text()
    except Exception as e:
        print("Error:", e)

    text = text.replace('\n', ' ')

    sentences = text.split('.')

    document = []
    for sentence in sentences:
        doc = merge_entities(nlp_custom(sentence))
        document.append(doc)

    return document

@transaction.atomic
def create_and_save_inverted_index(document):
    text = extract_text_from_pdf(document.document_path)
    sentences = text.split('.')
    inverted_index = defaultdict(list)
    inverted_index_lemma = defaultdict(list)
    stop_words = set(stopwords.words('english'))

    for sentence_index, sentence in enumerate(sentences, start=1):
        doc_details = DocDetails.objects.create(document=document, docdetail=sentence, position=sentence_index)
        tokens = sentence.lower().split()
        lemmatized_tokens = lemmatization(sentence)

        for token in tokens:
            if token in stop_words:
                continue
            term, created = Terms.objects.get_or_create(term=token)
            PostingLists.objects.create(term=term, docdetail=doc_details)
            inverted_index[token].append((term.term_id, doc_details.docdetail_id))
        
        for lemma in lemmatized_tokens:
            if lemma in stop_words:
                continue
            term_lemma, lemma_created = TermLemmas.objects.get_or_create(termlemma=lemma)
            PostingListLemmas.objects.create(termlemma=term_lemma, docdetail=doc_details)
            inverted_index_lemma[lemma].append((term_lemma.termlemma_id, doc_details.docdetail_id))

    for term, postings in inverted_index.items():
        term_obj = Terms.objects.get(term=term)
        for posting in postings:
            PostingLists.objects.create(term_id=posting[0], docdetail_id=posting[1])
    
    for lemma, postings in inverted_index_lemma.items():
        term_lemma_obj = TermLemmas.objects.get(termlemma=lemma)
        for posting in postings:
            PostingListLemmas.objects.create(termlemma_id=posting[0], docdetail_id=posting[1])


def articles(request):
    documents = Documents.objects.all()
    print(documents)
    
    context = []

    for document in documents:
        extracted_text = extract_text_from_pdf(document.document_path)

        truncated_text = extracted_text[:1000]

        article_data = {
            'doc_name': os.path.splitext(document.document_name)[0],
            'context': truncated_text + '...',
            'full_path': document.document_path,
            'id': document.document_id
        }

        context.append(article_data)

    return render(request, 'pages/articles.html', {'articles': context})

def detailArticle(request, document_id):
    
    document = get_object_or_404(Documents, document_id=document_id)
    extracted_text = extract_text_from_pdf(document.document_path)

    article_data = {
        'doc_name': os.path.splitext(document.document_name)[0],
        'full_text': extracted_text
    }

    return render(request, 'pages/detailArticle.html', {'article': article_data})

""" Ontologi """

def generate_ontology(doc_ontology):
    # Proses pembuatan ontologi
    ontology = """@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix coffee: <http://www.semanticweb.org/ariana/coffee#> .

    # Ontology Header
    <http://www.semanticweb.org/ariana/coffee#>
        rdf:type owl:Ontology ;
        owl:versionIRI <http://www.semanticweb.org/ariana/coffee#1.0> .

    # Classes
    """

    classes = set()
    object_properties = set()
    print(f'ini doc_ontology:{doc_ontology}')

    for sent in doc_ontology:
        prev_entity = None
        print(f'ini entitas: {sent.ents}')
        for ent in sent.ents:            
            if ent.label_ != '':
                if ent.label_ == 'VERB':
                    if prev_entity:
                        # Menambahkan range dan domain dengan melihat entitas sebelum dan sesudah entitas VERB
                        object_properties.add(ent.text)
                        ontology += f"""
                        <http://www.semanticweb.org/ariana/coffee#{ent.text.replace(" ", "_")}> rdfs:domain <http://www.semanticweb.org/ariana/coffee#{prev_entity["type"]}> .
                        """
                        # Next entity
                        next_entity = None
                        for next_ent in sent.ents:
                            if next_ent.start > ent.end:
                                next_entity = next_ent
                                break
                        if next_entity:
                            ontology += f"""
                            <http://www.semanticweb.org/ariana/coffee#{ent.text.replace(" ", "_")}> rdfs:range <http://www.semanticweb.org/ariana/coffee#{next_entity.label_}> .
                            """
                            # Individual - Object Property - Individual
                            ontology += f"""
                            <http://www.semanticweb.org/ariana/coffee#{prev_entity["text"].replace(" ", "_")}> coffee:{ent.text.replace(" ", "_")} <http://www.semanticweb.org/ariana/coffee#{next_entity.text.replace(" ", "_")}> .
                            """
                    prev_entity = None  # Reset prev_entity
                else:
                    prev_entity = {"text": ent.text, "type": ent.label_}
                    classes.add(ent.label_)

    for sent in doc_ontology:
        for ent in sent.ents:
            if ent.label_ != '':
                individual_name = ent.text.replace(" ", "_")
                if ent.label_ != 'VERB':
                    classes.add(ent.label_)
                    ontology += f"""
                    <http://www.semanticweb.org/ariana/coffee#{individual_name}> rdf:type <http://www.semanticweb.org/ariana/coffee#{ent.label_}> .
                    """
    return ontology

def save_ontology(ontology, file_name):
    owl_directory = os.path.join(settings.BASE_DIR, 'kms_app/owl_file')
    file_path = os.path.join(owl_directory, file_name)
    with open(file_path, "w") as output_file:
        output_file.write(ontology)

def get_extra_information(answer):
    response = ""

    query = f"""
    PREFIX coffee: <http://www.semanticweb.org/ariana/coffee#>
    SELECT ?p ?o ?s WHERE {{
      {{ coffee:{answer} ?p ?o.
        FILTER (!CONTAINS(LCASE(STR(?p)), "type"))
      }}
      UNION
      {{ ?s ?p coffee:{answer}.
        FILTER (!CONTAINS(LCASE(STR(?p)), "type"))
      }}
    }}
    """

    try:
        results = get_fuseki_data(query)
    except Exception as e:
        print(f"Error executing query: {e}")
        return "Error executing query"

    if results:
        for row in results:
            predicate_name = row.get('p', '').split('#')[-1].replace("_", " ") if row.get('p') else None
            object_name = row.get('o', '').split('#')[-1].replace("_", " ") if row.get('o') else None
            subject_name = row.get('s', '').split('#')[-1].replace("_", " ") if row.get('s') else None

            if object_name:
                response += f"{answer} {predicate_name} {object_name}. "
            if subject_name:
                response += f"{subject_name} {predicate_name} {answer}. "

        return response
    else:
        return None


def get_annotation(question,annotation):

    keywords_nouns = pos_tagging_and_extract_nouns_ontology(question)

    noun = "_".join(keywords_nouns)
    print(noun)

    response = ""

    query = f"""
    PREFIX coffee: <http://www.semanticweb.org/ariana/coffee#>
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    SELECT ?s WHERE {{
      coffee:{noun} rdfs:{annotation[0]} ?s
    }}
    """

    try:
        results = get_fuseki_data(query)
    except Exception as e:
        print(f"Error executing query: {e}")
        return "Error executing query"

    if results:
        for row in results:
          response = row['s'].replace("\n", "<br>")
    else:
        response = "Tidak ada jawaban"

    return response