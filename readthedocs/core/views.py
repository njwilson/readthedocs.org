"""Core views, including the main homepage, post-commit build hook,
documentation and header rendering, and server errors.
"""

from django.core.mail import mail_admins
from django.core.urlresolvers import reverse
from django.core.cache import cache
from django.conf import settings
from django.db.models import F, Max
from django.http import HttpResponse, HttpResponseRedirect, \
    HttpResponsePermanentRedirect, Http404, HttpResponseNotFound
from django.shortcuts import render_to_response, get_object_or_404
from django.template import RequestContext
from django.views.decorators.csrf import csrf_view_exempt
from django.views.static import serve

from projects.models import Project, ImportedFile
from projects.tasks import update_docs

import json
import mimetypes
import os
import logging

log = logging.getLogger(__name__)

def homepage(request):
    #latest_projects = Project.objects.filter(builds__isnull=False).annotate(max_date=Max('builds__date')).order_by('-max_date')[:10]
    latest_projects = Project.objects.order_by('-modified_date')[:10]
    featured = Project.objects.filter(featured=True)

    return render_to_response('homepage.html',
                              {'project_list': latest_projects,
                               'featured_list': featured,
                               #'updated_list': updated
                               },
                context_instance=RequestContext(request))

def random_page(request, project=None):
    if project:
        return HttpResponseRedirect(ImportedFile.objects.filter(project__slug=project).order_by('?')[0].get_absolute_url())
    return HttpResponseRedirect(ImportedFile.objects.order_by('?')[0].get_absolute_url())

@csrf_view_exempt
def github_build(request):
    """
    A post-commit hook for github.
    """
    if request.method == 'POST':
        obj = json.loads(request.POST['payload'])
        name = obj['repository']['name']
        url = obj['repository']['url']
        ghetto_url = url.replace('http://', '').replace('https://', '')
        branch = obj['ref'].replace('refs/heads/', '')
        log.info("(Github Build) %s:%s" % (ghetto_url, branch))
        version_pk = None
        version_slug = branch
        try:
            project = Project.objects.filter(repo__contains=ghetto_url)[0]
            version = project.version_from_branch_name(branch)
            if version:
                default = project.default_branch or project.vcs_repo().fallback_branch
                if branch == default:
                    #Shortcircuit versions that are default
                    #These will build at "latest", and thus won't be active
                    version = project.versions.get(slug='latest')
                    version_pk = version.pk
                    version_slug = version.slug
                    log.info("(Github Build) Building %s:%s" % (project.slug, version.slug))
                elif version in project.versions.exclude(active=True):
                    log.info("(Github Build) Not building %s" % version.slug)
                    return HttpResponseNotFound('Not Building: %s' % branch)
                else:
                    version_pk = version.pk
                    version_slug = version.slug
                    log.info("(Github Build) Building %s:%s" % (project.slug, version.slug))
            else:
                version_slug = 'latest'
                branch = 'latest'
                log.info("(Github Build) Building %s:latest" % project.slug)
            #version_pk being None means it will use "latest"
            update_docs.delay(pk=project.pk, version_pk=version_pk, force=True)
            return HttpResponse('Build Started: %s' % version_slug)
        except Exception, e:
            mail_admins('Build Failure', '%s failed to build via github.\n\n%s' % (name, e))
            return HttpResponseNotFound('Build Failed')
    else:
        return render_to_response('post_commit.html', {},
                context_instance=RequestContext(request))

@csrf_view_exempt
def bitbucket_build(request):
    if request.method == 'POST':
        obj = json.loads(request.POST['payload'])
        rep = obj['repository']
        name = rep['name']
        url = "%s%s" % ("bitbucket.org",  rep['absolute_url'].rstrip('/'))
        log.info("(Bitbucket Build) %s" % (url))
        try:
            project = Project.objects.filter(repo__contains=url)[0]
            update_docs.delay(pk=project.pk, force=True)
            return HttpResponse('Build Started')
        except Exception, e:
            mail_admins('Build Failure', '%s failed to build via github.\n\n%s' % (name, e))
            return HttpResponseNotFound('Build Failed')
    else:
        return render_to_response('post_commit.html', {},
                context_instance=RequestContext(request))

@csrf_view_exempt
def generic_build(request, pk):
    project = Project.objects.get(pk=pk)
    context = {'built': False, 'project': project}
    if request.method == 'POST':
        context['built'] = True
        slug = request.POST.get('version_slug', None)
        if slug:
            version = project.versions.get(slug=slug)
            update_docs.delay(pk=pk, version_pk=version.pk, force=True)
        else:
            update_docs.delay(pk=pk, force=True)
        return HttpResponse('Build Started')
    return render_to_response('post_commit.html', context,
            context_instance=RequestContext(request))


def legacy_serve_docs(request, username, project_slug, filename):
    proj = get_object_or_404(Project, slug=project_slug)
    default_version = proj.get_default_version()
    url = reverse(serve_docs, kwargs={
        'project_slug': project_slug,
        'version_slug': default_version,
        'lang_slug': 'en',
        'filename': filename
    })
    return HttpResponsePermanentRedirect(url)


def serve_docs(request, lang_slug, version_slug, filename, project_slug=None):
    if not project_slug:
        project_slug = request.slug
    proj = get_object_or_404(Project, slug=project_slug)
    if not version_slug or not lang_slug:
        version_slug = proj.get_default_version()
        url = reverse(serve_docs, kwargs={
            'project_slug': project_slug,
            'version_slug': version_slug,
            'lang_slug': 'en',
            'filename': filename
        })
        return HttpResponseRedirect(url)
    if not filename:
        filename = "index.html"
    elif proj.documentation_type == 'sphinx_htmldir' and "_static" not in filename and "html" not in filename and not "inv" in filename:
        filename += "index.html"
    else:
        filename = filename.rstrip('/')
    basepath = proj.rtd_build_path(version_slug)
    if not settings.DEBUG:
        fullpath = os.path.join(basepath, filename)
        mimetype, encoding = mimetypes.guess_type(fullpath)
        mimetype = mimetype or 'application/octet-stream'
        response = HttpResponse(mimetype=mimetype)
        if encoding:
            response["Content-Encoding"] = encoding
        try:
            response['X-Accel-Redirect'] = os.path.join('/user_builds',
                                             proj.slug,
                                             'rtd-builds',
                                             version_slug,
                                             filename)
        except UnicodeEncodeError:
            raise Http404

        return response
    else:
        return serve(request, filename, basepath)

def server_error(request, template_name='500.html'):
    """
    A simple 500 handler so we get media
    """
    r = render_to_response(template_name,
        context_instance = RequestContext(request)
    )
    r.status_code = 500
    return r

def server_error_404(request, template_name='404.html'):
    """
    A simple 500 handler so we get media
    """
    r =  render_to_response(template_name,
        context_instance = RequestContext(request)
    )
    r.status_code = 404
    return r
