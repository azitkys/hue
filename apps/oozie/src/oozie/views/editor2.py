#!/usr/bin/env python
# Licensed to Cloudera, Inc. under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  Cloudera, Inc. licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging

from django.core.urlresolvers import reverse
from django.forms.formsets import formset_factory
from django.http import HttpResponse
from django.shortcuts import redirect
from django.utils.translation import ugettext as _

from desktop.lib.django_util import render
from desktop.lib.exceptions_renderable import PopupException
from desktop.lib.i18n import smart_str
from desktop.lib.rest.http_client import RestException
from desktop.models import Document2

from liboozie.credentials import Credentials
from liboozie.oozie_api import get_oozie
from liboozie.submission2 import Submission

from oozie.forms import ParameterForm
from oozie.models2 import Workflow, Coordinator, NODES, WORKFLOW_NODE_PROPERTIES, import_workflows_from_hue_3_7


LOG = logging.getLogger(__name__)



def list_editor_workflows(request):
  workflows = Document2.objects.filter(type='oozie-workflow2', owner=request.user)

  return render('editor/list_editor_workflows.mako', request, {
      'workflows': workflows
  })


def edit_workflow(request):

  workflow_id = request.GET.get('workflow')
  
  if workflow_id:
    workflow = Workflow(document=Document2.objects.get(id=workflow_id)) # Todo perms
  else:
    workflow = Workflow()
  
  workflow_data = workflow.get_data()

  api = get_oozie(request.user)
  credentials = Credentials()
  
  try:  
    credentials.fetch(api)
  except Exception, e:
    LOG.error(smart_str(e))

  return render('editor/workflow_editor.mako', request, {
      'layout_json': json.dumps(workflow_data['layout']),
      'workflow_json': json.dumps(workflow_data['workflow']),
      'credentials_json': json.dumps(credentials.credentials.keys()),
      'workflow_properties_json': json.dumps(WORKFLOW_NODE_PROPERTIES),
  })


def new_workflow(request):
  return edit_workflow(request)


def save_workflow(request):
  response = {'status': -1}

  workflow = json.loads(request.POST.get('workflow', '{}')) # TODO perms
  layout = json.loads(request.POST.get('layout', '{}'))

  name = 'test'

  if workflow.get('id'):
    workflow_doc = Document2.objects.get(id=workflow['id'])
  else:      
    workflow_doc = Document2.objects.create(name=name, type='oozie-workflow2', owner=request.user)

  subworkflows = [node['properties']['subworkflow'] for node in workflow['nodes'] if node['type'] == 'subworkflow-widget']
  if subworkflows:
    dependencies = Document2.objects.filter(uuid__in=subworkflows)
    workflow_doc.dependencies = dependencies

  workflow_doc.update_data({'workflow': workflow})
  workflow_doc.update_data({'layout': layout})
  workflow_doc.name = name
  workflow_doc.save()
  
  workflow_instance = Workflow(document=workflow_doc)
  workflow_instance.check_workspace(request.fs)
  
  response['status'] = 0
  response['id'] = workflow_doc.id
  response['message'] = _('Page saved !')

  return HttpResponse(json.dumps(response), mimetype="application/json")


def new_node(request):
  response = {'status': -1}

  workflow = json.loads(request.POST.get('workflow', '{}')) # TODO perms
  node = json.loads(request.POST.get('node', '{}'))

  properties = NODES[node['widgetType']].get_mandatory_fields()
  workflows = []

  if node['widgetType'] == 'subworkflow-widget':
    workflows = [{
        'name': workflow.name,
        'owner': workflow.owner.username,
        'value': workflow.uuid
      } for workflow in Document2.objects.filter(type='oozie-workflow2', owner=request.user)
    ]
    
  response['status'] = 0
  response['properties'] = properties 
  response['workflows'] = workflows
  
  return HttpResponse(json.dumps(response), mimetype="application/json")


def add_node(request):
  response = {'status': -1}

  workflow = json.loads(request.POST.get('workflow', '{}')) # TODO perms
  node = json.loads(request.POST.get('node', '{}'))
  properties = json.loads(request.POST.get('properties', '{}'))
  subworkflow = json.loads(request.POST.get('subworkflow', '{}'))

  _properties = dict(NODES[node['widgetType']].get_fields())
  _properties.update(dict([(_property['name'], _property['value']) for _property in properties]))

  if subworkflow:
    _properties.update({
       'workflow': subworkflow['value']
    })

  response['status'] = 0
  response['properties'] = _properties
  response['name'] = '%s-%s' % (node['widgetType'].split('-')[0], node['id'][:4])

  return HttpResponse(json.dumps(response), mimetype="application/json")


def gen_xml_workflow(request):
  response = {'status': -1}

  try:
    workflow_json = json.loads(request.POST.get('workflow', '{}')) # TODO perms
  
    workflow = Workflow(workflow=workflow_json)
  
    response['status'] = 0
    response['xml'] = workflow.to_xml()
  except Exception, e:
    response['message'] = str(e)
    
  return HttpResponse(json.dumps(response), mimetype="application/json") 


def submit_workflow(request, doc_id):
  workflow = Workflow(document=Document2.objects.get(id=doc_id)) # Todo perms
  ParametersFormSet = formset_factory(ParameterForm, extra=0)

  if request.method == 'POST':
    params_form = ParametersFormSet(request.POST)    

    if params_form.is_valid():
      mapping = dict([(param['name'], param['value']) for param in params_form.cleaned_data])

      job_id = _submit_workflow(request.user, request.fs, request.jt, workflow, mapping)

      request.info(_('Workflow submitted'))
      return redirect(reverse('oozie:list_oozie_workflow', kwargs={'job_id': job_id}))
    else:
      request.error(_('Invalid submission form: %s' % params_form.errors))
  else:
    parameters = workflow.find_all_parameters()
    initial_params = ParameterForm.get_initial_params(dict([(param['name'], param['value']) for param in parameters]))
    params_form = ParametersFormSet(initial=initial_params)

  popup = render('editor/submit_job_popup.mako', request, {
                   'params_form': params_form,
                   'action': reverse('oozie:editor_submit_workflow', kwargs={'doc_id': workflow.id})
                 }, force_template=True).content
  return HttpResponse(json.dumps(popup), mimetype="application/json")


def _submit_workflow(user, fs, jt, workflow, mapping):
  try:
    submission = Submission(user, workflow, fs, jt, mapping)
    job_id = submission.run()
    return job_id
  except RestException, ex:
    detail = ex._headers.get('oozie-error-message', ex)
    if 'Max retries exceeded with url' in str(detail):
      detail = '%s: %s' % (_('The Oozie server is not running'), detail)
    LOG.error(smart_str(detail))
    raise PopupException(_("Error submitting workflow %s") % (workflow,), detail=detail)

  return redirect(reverse('oozie:list_oozie_workflow', kwargs={'job_id': job_id}))


def import_hue_3_7_workflows(request):
  response = {'status': -1}

  try:
    response['status'] = 0
    response['json'] = import_workflows_from_hue_3_7().to_xml()
  except Exception, e:
    response['message'] = str(e)
    
  return HttpResponse(json.dumps(response), mimetype="application/json") 



def list_editor_coordinators(request):
  coordinators = Document2.objects.filter(type='oozie-coordinator2', owner=request.user)

  return render('editor/list_editor_coordinators.mako', request, {
      'coordinators': coordinators
  })


def edit_coordinator(request):
  coordinator_id = request.GET.get('coordinator')
  
  if coordinator_id:
    coordinator = Coordinator(document=Document2.objects.get(id=coordinator_id)) # Todo perms
  else:
    coordinator = Coordinator()

  api = get_oozie(request.user)
  credentials = Credentials()
  
  try:  
    credentials.fetch(api)
  except Exception, e:
    LOG.error(smart_str(e))

  return render('editor/coordinator_editor.mako', request, {
      'coordinator_json': coordinator.json,
      'credentials_json': json.dumps(credentials.credentials.keys()),
      'workflows_json': json.dumps(list(Document2.objects.filter(type='oozie-workflow2', owner=request.user).values('uuid', 'name')))
  })


def new_coordinator(request):
  return edit_coordinator(request)


def save_coordinator(request):
  response = {'status': -1}

  coordinator_data = json.loads(request.POST.get('coordinator', '{}')) # TODO perms

  name = 'test'

  if coordinator_data.get('id'):
    coordinator_doc = Document2.objects.get(id=coordinator_data['id'])
  else:      
    coordinator_doc = Document2.objects.create(name=name, type='oozie-coordinator2', owner=request.user)

  if coordinator_data['properties']['workflow']:
    dependencies = Document2.objects.filter(uuid=coordinator_data['properties']['workflow'])
    coordinator_doc.dependencies = dependencies

  coordinator_doc.update_data(coordinator_data)
  coordinator_doc.name = name
  coordinator_doc.save()
  
  response['status'] = 0
  response['id'] = coordinator_doc.id
  response['message'] = _('Saved !')

  return HttpResponse(json.dumps(response), mimetype="application/json")


def gen_xml_coordinator(request):
  response = {'status': -1}

#  try:
  coordinator_dict = json.loads(request.POST.get('coordinator', '{}')) # TODO perms

  coordinator = Coordinator(data=coordinator_dict)

  response['status'] = 0
  response['xml'] = coordinator.to_xml()
#  except Exception, e:
#    response['message'] = str(e)
    
  return HttpResponse(json.dumps(response), mimetype="application/json") 


def submit_coordinator(request, coordinator):
  ParametersFormSet = formset_factory(ParameterForm, extra=0)

  if request.method == 'POST':
    params_form = ParametersFormSet(request.POST)

    if params_form.is_valid():
      mapping = dict([(param['name'], param['value']) for param in params_form.cleaned_data])
      job_id = _submit_coordinator(request, coordinator, mapping)

      request.info(_('Coordinator submitted.'))
      return redirect(reverse('oozie:list_oozie_coordinator', kwargs={'job_id': job_id}))
    else:
      request.error(_('Invalid submission form: %s' % params_form.errors))
  else:
    parameters = coordinator.find_all_parameters()
    initial_params = ParameterForm.get_initial_params(dict([(param['name'], param['value']) for param in parameters]))
    params_form = ParametersFormSet(initial=initial_params)

  popup = render('editor/submit_job_popup.mako', request, {
                 'params_form': params_form,
                 'action': reverse('oozie:submit_coordinator',  kwargs={'coordinator': coordinator.id})
                }, force_template=True).content
  return HttpResponse(json.dumps(popup), mimetype="application/json")


def _submit_coordinator(request, coordinator, mapping):
  try:
    wf_dir = Submission(request.user, coordinator.workflow, request.fs, request.jt, mapping).deploy()

    properties = {'wf_application_path': request.fs.get_hdfs_path(wf_dir)}
    properties.update(mapping)

    submission = Submission(request.user, coordinator, request.fs, request.jt, properties=properties)
    job_id = submission.run()

    History.objects.create_from_submission(submission)

    return job_id
  except RestException, ex:
    raise PopupException(_("Error submitting coordinator %s") % (coordinator,),
                         detail=ex._headers.get('oozie-error-message', ex))